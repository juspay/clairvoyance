"""
Exotel recording download functionality
"""

from io import BytesIO
from typing import Optional

import aiohttp

from app.ai.voice.agents.breeze_buddy.services.telephony.recording_download import (
    download_recording_via_ssrf,
)
from app.core.config.static import EXOTEL_API_KEY, EXOTEL_API_TOKEN

# Hosts the Exotel API credentials may be sent to (global + India regional).
# Exotel also returns credential-less presigned object-store URLs, so its
# recording host is not pinned: any public URL is fetched, without keys.
_RECORDING_HOSTS = ["exotel.com", "exotel.in"]


def is_recording_url(recording_url: str) -> bool:
    """Any URL: Exotel's recording host is not pinned (see above)."""
    return True


async def download_call_recording(
    recording_url: str, call_sid: str
) -> Optional[BytesIO]:
    """Download an Exotel recording into memory (keys go only to Exotel hosts)."""

    async def _auth() -> aiohttp.BasicAuth:
        return aiohttp.BasicAuth(EXOTEL_API_KEY, EXOTEL_API_TOKEN)

    return await download_recording_via_ssrf(
        recording_url,
        call_sid,
        provider="Exotel",
        recording_hosts=_RECORDING_HOSTS,
        make_auth=_auth,
        any_public_host=True,
    )
