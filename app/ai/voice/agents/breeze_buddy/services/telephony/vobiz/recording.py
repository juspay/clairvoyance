"""
Vobiz call recording: the ``<Record>`` element for the answer XML, and the
download of the finished recording.
"""

from html import escape as html_escape
from io import BytesIO
from typing import Dict, List, Optional
from urllib.parse import quote, urlsplit

from app.ai.voice.agents.breeze_buddy.services.telephony.recording_download import (
    download_recording_via_ssrf,
    is_on_recording_hosts,
)
from app.core.config.static import (
    APP_BASE_URL,
    VOBIZ_API_BASE_URL,
    VOBIZ_AUTH_ID,
    VOBIZ_AUTH_TOKEN,
    VOBIZ_RECORDING_TIME_LIMIT,
)

# Vobiz may serve a WAV file under an .mp3 URL; a WAV starts with "RIFF".
_WAV_MAGIC = b"RIFF"
# The configured REST host (VOBIZ_API_BASE_URL); api.vobiz.ai by default.
_VOBIZ_API_HOST = urlsplit(VOBIZ_API_BASE_URL).hostname


def _recording_callback_url(call_uuid: str) -> str:
    """Where Vobiz POSTs RecordStop. call_uuid rides the query string, as on Plivo."""
    return (
        f"{APP_BASE_URL}/agent/voice/breeze-buddy/vobiz/callback/details"
        f"?call_uuid={quote(call_uuid, safe='')}"
    )


def _recording_hosts() -> List[str]:
    """Where a recording URL may carry the X-Auth keys: *.vobiz.ai and the
    configured REST host, over https. Any other URL but our copy is refused."""
    return [host for host in ("vobiz.ai", _VOBIZ_API_HOST) if host]


def is_recording_url(recording_url: str) -> bool:
    """True if ``recording_url`` may be stored and fetched as a Vobiz recording."""
    return is_on_recording_hosts(recording_url, _recording_hosts())


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

    async def _auth() -> Dict[str, str]:
        return {"X-Auth-ID": VOBIZ_AUTH_ID, "X-Auth-Token": VOBIZ_AUTH_TOKEN}

    return await download_recording_via_ssrf(
        recording_url,
        call_sid,
        provider="Vobiz",
        recording_hosts=_recording_hosts(),
        make_auth=_auth,
    )
