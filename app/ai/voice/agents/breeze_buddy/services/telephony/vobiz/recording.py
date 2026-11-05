"""
Vobiz call recording: the ``<Record>`` element for the answer XML, and the
download of the finished recording.
"""

import asyncio
from html import escape as html_escape
from io import BytesIO
from typing import Optional
from urllib.parse import quote, urljoin, urlsplit

import aiohttp

from app.core.config.static import (
    APP_BASE_URL,
    VOBIZ_API_BASE_URL,
    VOBIZ_AUTH_ID,
    VOBIZ_AUTH_TOKEN,
    VOBIZ_RECORDING_TIME_LIMIT,
)
from app.core.logger import logger
from app.core.transport.http_client import get_proxy_config

# Vobiz may serve a WAV file under an .mp3 URL; a WAV starts with "RIFF".
_WAV_MAGIC = b"RIFF"
# Besides https *.vobiz.ai, the REST host on its own scheme (the local harness
# points it at an http stand-in).
_VOBIZ_API_HOST = urlsplit(VOBIZ_API_BASE_URL).hostname
_VOBIZ_API_SCHEME = urlsplit(VOBIZ_API_BASE_URL).scheme
_MAX_RECORDING_REDIRECTS = 5
# One deadline for the whole download, every redirect hop included (a
# session ClientTimeout is per request). Console playback awaits this, so a
# silent media host must not hold it for aiohttp's 300 s default.
_DOWNLOAD_TIMEOUT_SECONDS = 60
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _recording_callback_url(call_uuid: str) -> str:
    """Where Vobiz POSTs RecordStop. call_uuid rides the query string, as on Plivo."""
    return (
        f"{APP_BASE_URL}/agent/voice/breeze-buddy/vobiz/callback/details"
        f"?call_uuid={quote(call_uuid, safe='')}"
    )


def _vobiz_auth_headers(url: str) -> dict[str, str]:
    """Vobiz's X-Auth headers if ``url`` is on a Vobiz host, else none."""
    # The URL comes from an unauthenticated callback: never send credentials off
    # Vobiz, nor over cleartext to a vobiz.ai host.
    parts = urlsplit(url)
    host = parts.hostname or ""
    on_vobiz = parts.scheme == "https" and (
        host == "vobiz.ai" or host.endswith(".vobiz.ai")
    )
    on_api = host == _VOBIZ_API_HOST and parts.scheme == _VOBIZ_API_SCHEME
    if on_vobiz or on_api:
        return {"X-Auth-ID": VOBIZ_AUTH_ID, "X-Auth-Token": VOBIZ_AUTH_TOKEN}
    return {}


def vobiz_record_xml(call_uuid: str) -> str:
    """``<Record>`` element that records the whole call in the background.

    Goes before ``<Stream>`` (recordSession returns immediately; Stream holds
    the call). Vobiz's defaults are set explicitly because they differ from
    what a session recording needs: ``playBeep`` defaults to true (the
    customer would hear a beep before the greeting), and ``maxLength`` and the
    silence ``timeout`` both default to 60 s. ``redirect="false"`` keeps the
    recording callback from taking over call control.
    """
    callback_url = html_escape(_recording_callback_url(call_uuid))
    return (
        f'<Record recordSession="true" redirect="false" playBeep="false" '
        f'fileFormat="mp3" maxLength="{VOBIZ_RECORDING_TIME_LIMIT}" '
        f'timeout="{VOBIZ_RECORDING_TIME_LIMIT}" '
        f'callbackUrl="{callback_url}" callbackMethod="POST"/>'
    )


def recording_file_format(audio: bytes) -> str:
    """Return "wav" or "mp3", from the bytes — Vobiz's file extension can lie."""
    return "wav" if audio[:4] == _WAV_MAGIC else "mp3"


async def download_call_recording(
    recording_url: str, call_sid: str
) -> Optional[BytesIO]:
    """Download a Vobiz recording into memory (media.vobiz.ai needs auth headers)."""
    try:
        proxy_url = get_proxy_config()
        logger.info(f"Downloading Vobiz recording from: {recording_url}")
        url = recording_url
        async with asyncio.timeout(_DOWNLOAD_TIMEOUT_SECONDS):
            async with aiohttp.ClientSession() as session:
                # Redirects are followed by hand: aiohttp forwards custom headers
                # (X-Auth-*) cross-origin, so each hop gets its own header decision.
                for _ in range(_MAX_RECORDING_REDIRECTS + 1):
                    async with session.get(
                        url,
                        headers=_vobiz_auth_headers(url),
                        proxy=proxy_url,
                        allow_redirects=False,
                    ) as response:
                        location = response.headers.get("Location")
                        if response.status in _REDIRECT_STATUSES and location:
                            url = urljoin(url, location)
                            continue
                        if response.status != 200:
                            logger.error(
                                "Failed to download Vobiz recording. "
                                f"Status: {response.status}"
                            )
                            return None
                        audio_data = await response.read()
                        break
                else:
                    logger.error(
                        f"Too many redirects downloading Vobiz recording: {recording_url}"
                    )
                    return None
        logger.info(
            f"Successfully downloaded Vobiz recording for call: {call_sid} "
            f"({len(audio_data)} bytes)"
        )
        return BytesIO(audio_data)
    except TimeoutError:
        logger.error(
            f"Timed out after {_DOWNLOAD_TIMEOUT_SECONDS}s downloading Vobiz "
            f"recording: {recording_url}"
        )
        return None
    except Exception as e:
        logger.opt(exception=e).error(f"Error downloading Vobiz recording: {e}")
        return None
