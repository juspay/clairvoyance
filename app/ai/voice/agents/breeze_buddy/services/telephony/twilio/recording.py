"""
Twilio recording download functionality
"""

from io import BytesIO
from typing import Optional

import aiohttp

from app.core.config.static import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
from app.core.logger import logger
from app.core.security.ssrf import ssrf_safe_request
from app.core.transport.http_client import get_proxy_config

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
        auth = aiohttp.BasicAuth(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

        # Get proxy configuration
        proxy_url = get_proxy_config()

        logger.info(f"Downloading Twilio recording from: {recording_url}")

        # SSRF: never send the master Twilio credentials to a host that is not
        # Twilio's own, and never to an internal/metadata address, even if a
        # forged webhook supplied the URL (PT-05). Auth is stripped on any
        # off-allow-list redirect.
        async with aiohttp.ClientSession() as session:
            async with ssrf_safe_request(
                session,
                "GET",
                recording_url,
                auth=auth,
                allowed_host_suffixes=_TWILIO_HOST_SUFFIXES,
                proxy=proxy_url,
            ) as response:
                if response.status != 200:
                    logger.error(
                        f"Failed to download Twilio recording. Status: {response.status}"
                    )
                    return None

                # Read the recording into memory
                audio_data = await response.read()
                audio_file = BytesIO(audio_data)

        logger.info(
            f"Successfully downloaded Twilio recording for call: {call_sid} ({len(audio_data)} bytes)"
        )
        return audio_file

    except Exception as e:
        logger.error(f"Error downloading Twilio recording: {e}", exc_info=True)
        return None
