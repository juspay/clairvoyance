"""
Shared SSRF-safe download for provider call recordings.

Every provider's recording URL arrives from an unsigned provider callback, so it
is attacker-influenceable. This one path keeps the credential-scoping identical
across Twilio, Exotel and Plivo instead of repeating it per provider.
"""

from io import BytesIO
from typing import Awaitable, Callable, Optional, Sequence

import aiohttp

from app.core.logger import logger
from app.core.security.ssrf import (
    SSRFError,
    host_matches_allowlist,
    redact_url,
    ssrf_safe_request,
)
from app.core.transport.http_client import get_proxy_config

# A recording may chain a few hops (api host -> regional -> CDN -> signed
# object), so keep headroom over the obvious two. One budget covers the whole
# chain, matching Vobiz, instead of aiohttp's 300s default.
_MAX_RECORDING_REDIRECTS = 8
_DOWNLOAD_TIMEOUT_SECONDS = 60


async def download_recording_via_ssrf(
    recording_url: str,
    call_sid: str,
    *,
    provider: str,
    recording_hosts: Sequence[str],
    make_auth: Callable[[], Awaitable[aiohttp.BasicAuth]],
) -> Optional[BytesIO]:
    """Download a provider recording into memory, scoping credentials safely.

    Credentials from ``make_auth`` are sent ONLY when the URL is an https URL on
    ``recording_hosts`` (the provider's own hosts). Every hop goes through the
    SSRF egress rules (blocking internal / metadata targets) and redirects are
    followed manually, dropping the credentials the moment the chain leaves the
    initial host. A provider may also hand back a credential-less presigned
    object-store URL (any host, any scheme); that is fetched without credentials,
    so no secret can leak regardless of where it points.

    ``make_auth`` is awaited only on the credentialed path, so a provider whose
    credentials need a database/account lookup does no work for a request that
    will not carry them.

    Note: ``recording_hosts`` gates the INITIAL host only. ssrf_safe_request
    drops credentials on any host change, so a redirect between two listed hosts
    is still credential-less — the list is "where a recording URL may START with
    keys", not "where keys may be re-sent".
    """
    try:
        is_provider_host = recording_url.startswith(
            "https://"
        ) and host_matches_allowlist(recording_url, list(recording_hosts))
        auth = await make_auth() if is_provider_host else None
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

                audio_data = await response.read()
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
