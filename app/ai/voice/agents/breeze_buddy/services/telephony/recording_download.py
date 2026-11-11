"""
Shared SSRF-safe download for provider call recordings.

Every provider's recording URL arrives from an unsigned provider callback, so it
is attacker-influenceable. This one path keeps the credential-scoping identical
across Twilio, Exotel, Plivo and Vobiz instead of repeating it per provider.
"""

from io import BytesIO
from typing import Awaitable, Callable, Dict, Optional, Sequence, Union
from urllib.parse import urlsplit

import aiohttp

from app.core.logger import logger
from app.core.security.ssrf import (
    SSRFError,
    host_matches_allowlist,
    redact_url,
    ssrf_safe_request,
)
from app.core.transport.http_client import get_proxy_config
from app.services.gcp.storage.storage import GCS_PUBLIC_BASE_URL

# A recording may chain a few hops (api host -> regional -> CDN -> signed
# object), so keep headroom over the obvious two. One budget covers the whole
# chain, matching Vobiz, instead of aiohttp's 300s default.
_MAX_RECORDING_REDIRECTS = 8
_DOWNLOAD_TIMEOUT_SECONDS = 60
# Plivo and Vobiz record a call for up to 4 h (*_RECORDING_TIME_LIMIT): ~460 MB
# as 8 kHz 16-bit dual-channel WAV, the largest format a provider serves.
# Anything larger is not a call recording.
_MAX_RECORDING_BYTES = 512 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024

# Our uploaded copy: update_call_recording stores it, GET recording re-fetches it.
_OUR_RECORDING_HOSTS = [urlsplit(GCS_PUBLIC_BASE_URL).hostname or ""]

# Basic auth (Twilio, Exotel, Plivo) or auth headers (Vobiz's X-Auth-ID/Token).
RecordingCredentials = Union[aiohttp.BasicAuth, Dict[str, str]]


def is_on_recording_hosts(recording_url: str, recording_hosts: Sequence[str]) -> bool:
    """True if ``recording_url`` names one of ``recording_hosts`` or our own
    uploaded copy. Any other host is a file the callback's sender chose, so it
    is neither fetched nor stored as the call's recording."""
    return host_matches_allowlist(
        recording_url, [*recording_hosts, *_OUR_RECORDING_HOSTS]
    )


async def download_recording_via_ssrf(
    recording_url: str,
    call_sid: str,
    *,
    provider: str,
    recording_hosts: Sequence[str],
    make_auth: Callable[[], Awaitable[RecordingCredentials]],
    any_public_host: bool = False,
) -> Optional[BytesIO]:
    """Download a provider recording into memory, scoping credentials safely.

    The URL must name one of ``recording_hosts`` (the provider's own hosts) or
    our uploaded copy; any other URL is refused before a request is made.
    ``any_public_host`` lifts that for a provider whose recording host is not
    known (Exotel, which also hands back presigned object-store URLs): any
    public URL is then fetched, without credentials.

    Credentials from ``make_auth`` are sent ONLY when the URL is an https URL on
    ``recording_hosts``. Every hop goes through the SSRF egress rules (blocking
    internal / metadata targets) and redirects are followed manually, dropping
    the credentials the moment the chain leaves the initial host. A body over
    ``_MAX_RECORDING_BYTES`` is dropped.

    ``make_auth`` is awaited only on the credentialed path, so a provider whose
    credentials need a database/account lookup does no work for a request that
    will not carry them.

    Note: ``recording_hosts`` gates the INITIAL host only. ssrf_safe_request
    drops credentials on any host change, so a redirect between two listed hosts
    is still credential-less — the list is "where a recording URL may START with
    keys", not "where keys may be re-sent".

    Header credentials follow the same rules: ssrf_safe_request strips every
    caller header but a few content-negotiation ones on any host change.
    """
    try:
        if not any_public_host and not is_on_recording_hosts(
            recording_url, recording_hosts
        ):
            logger.warning(
                f"Refusing {provider} recording off its hosts: "
                f"{redact_url(recording_url)}"
            )
            return None

        is_provider_host = recording_url.startswith(
            "https://"
        ) and host_matches_allowlist(recording_url, list(recording_hosts))
        credentials = await make_auth() if is_provider_host else None
        auth = credentials if isinstance(credentials, aiohttp.BasicAuth) else None
        headers = credentials if isinstance(credentials, dict) else None
        proxy_url = get_proxy_config()

        logger.info(
            f"Downloading {provider} recording from: {redact_url(recording_url)}"
        )

        timeout = aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with ssrf_safe_request(
                session,
                "GET",
                recording_url,
                auth=auth,
                headers=headers,
                allowed_host_suffixes=(
                    list(recording_hosts) if is_provider_host else None
                ),
                allow_http=not is_provider_host,
                max_redirects=_MAX_RECORDING_REDIRECTS,
                proxy=proxy_url,
            ) as response:
                if response.status != 200:
                    logger.error(
                        f"Failed to download {provider} recording. Status: {response.status}"
                    )
                    return None

                audio_data = bytearray()
                async for chunk in response.content.iter_chunked(_READ_CHUNK_BYTES):
                    audio_data += chunk
                    if len(audio_data) > _MAX_RECORDING_BYTES:
                        logger.warning(
                            f"Dropping {provider} recording for call {call_sid}: "
                            f"over {_MAX_RECORDING_BYTES} bytes"
                        )
                        return None
                audio_file = BytesIO(audio_data)

        logger.info(
            f"Successfully downloaded {provider} recording for call: {call_sid} "
            f"({len(audio_data)} bytes)"
        )
        return audio_file

    except SSRFError as e:
        logger.warning(f"Refusing to download {provider} recording: {e}")
        return None
    except Exception as e:
        logger.error(f"Error downloading {provider} recording: {e}", exc_info=True)
        return None
