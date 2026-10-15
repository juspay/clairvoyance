"""
Twilio recording download functionality
"""

from io import BytesIO
from typing import Optional

import aiohttp

from app.core.config.static import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
)
from app.core.logger import logger
from app.core.network import fetch_bytes_from_allowed_host, host_matches_allowlist

# Only ever send Twilio BasicAuth to Twilio's own hosts (recordings + media CDN).
_TWILIO_HOST_SUFFIXES = ("twilio.com", "twiliocdn.com")


async def download_call_recording(
    recording_url: str, call_sid: str
) -> Optional[BytesIO]:
    """
    Download a recording from Twilio directly into memory.

    Args:
        recording_url (str): The URL of the recording to download
        call_sid (str): The call SID for logging purposes

    Returns:
        Optional[BytesIO]: In-memory file object containing the recording, or None if download failed
    """
    try:
        # Twilio recordings require authentication
        auth = (
            aiohttp.BasicAuth(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            if host_matches_allowlist(recording_url, list(_TWILIO_HOST_SUFFIXES))
            else None
        )

        # No allow-list without credentials: the address check is what makes
        # the fetch safe, and gating reachability on a hostname turns a
        # recording served from anywhere unlisted into a silent failure.
        audio_data = await fetch_bytes_from_allowed_host(
            recording_url,
            allowed_host_suffixes=list(_TWILIO_HOST_SUFFIXES) if auth else None,
            auth=auth,
            # Nothing secret to protect without keys, so a provider serving the
            # file over plain http loses the recording rather than the argument.
            # With keys it stays https-only.
            allow_http=auth is None,
        )
        if audio_data is None:
            return None
        audio_file = BytesIO(audio_data)

        logger.info(
            f"Successfully downloaded Twilio recording for call: {call_sid} "
            f"({len(audio_data)} bytes)"
        )
        return audio_file

    except Exception as e:
        logger.error(f"Error downloading Twilio recording: {e}", exc_info=True)
        return None
