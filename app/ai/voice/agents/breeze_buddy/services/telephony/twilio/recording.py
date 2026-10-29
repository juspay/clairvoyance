"""
Twilio recording download functionality
"""

from io import BytesIO
from typing import Optional

import aiohttp

from app.ai.voice.agents.breeze_buddy.services.telephony.recording_hosts import (
    is_provider_recording_url,
)
from app.core.config.static import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
from app.core.logger import logger
from app.core.security.ssrf import redact_url, ssrf_safe_request
from app.core.transport.http_client import get_proxy_config


async def download_call_recording(
    recording_url: str, call_sid: str
) -> Optional[BytesIO]:
    """
    Download a recording from Twilio directly into memory.

    Every redirect hop is checked (ssrf_safe_request), so none can reach an
    internal or metadata address. The URL is logged redacted (redact_url): a
    recording URL can carry a signed query or token, and scheme, host and path
    are all a log line needs.

    Args:
        recording_url (str): The URL of the recording to download
        call_sid (str): The call SID for logging purposes

    Returns:
        Optional[BytesIO]: In-memory file object containing the recording, or None if download failed
    """
    try:
        # Twilio recordings require authentication. The URL comes from an
        # unauthenticated callback: the keys go only to Twilio over https, and
        # our self-hosted copy (or anything else) is fetched without them.
        auth = (
            aiohttp.BasicAuth(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            if is_provider_recording_url("twilio", recording_url)
            else None
        )

        # Get proxy configuration
        proxy_url = get_proxy_config()

        logger.info(f"Downloading Twilio recording from: {redact_url(recording_url)}")

        async with aiohttp.ClientSession() as session:
            # A redirect off Twilio's origin (its signed media URL) drops the keys.
            async with ssrf_safe_request(
                session, "GET", recording_url, auth=auth, proxy=proxy_url
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
