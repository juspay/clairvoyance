"""
Twilio recording download functionality
"""

from io import BytesIO
from typing import Optional

import aiohttp

from app.ai.voice.agents.breeze_buddy.services.telephony.recording_download import (
    download_recording_via_ssrf,
    is_on_recording_hosts,
)
from app.core.config.static import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN

# Host the Twilio account credentials may be sent to (recording URLs live on the
# API host and 302 to a CDN, which is followed without the keys).
_RECORDING_HOSTS = ["api.twilio.com"]


def is_recording_url(recording_url: str) -> bool:
    """True if ``recording_url`` may be stored and fetched as a Twilio recording."""
    return is_on_recording_hosts(recording_url, _RECORDING_HOSTS)


async def download_call_recording(
    recording_url: str, call_sid: str
) -> Optional[BytesIO]:
    """Download a Twilio recording into memory (keys go only to api.twilio.com)."""

    async def _auth() -> aiohttp.BasicAuth:
        return aiohttp.BasicAuth(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    return await download_recording_via_ssrf(
        recording_url,
        call_sid,
        provider="Twilio",
        recording_hosts=_RECORDING_HOSTS,
        make_auth=_auth,
    )
