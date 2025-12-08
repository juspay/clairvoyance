"""
Exotel recording download functionality
"""

from io import BytesIO
from typing import Optional

import aiohttp

from app.core.config.static import EXOTEL_API_KEY, EXOTEL_API_TOKEN
from app.core.logger import logger
from app.core.transport.http_client import get_proxy_config


async def download_call_recording(
    recording_url: str, call_sid: str
) -> Optional[BytesIO]:
    """
    Download a recording from Exotel directly into memory.

    Args:
        recording_url (str): The URL of the recording to download
        call_sid (str): The call SID for logging purposes

    Returns:
        Optional[BytesIO]: In-memory file object containing the recording, or None if download failed
    """
    try:
        # Exotel recordings require basic authentication
        auth = aiohttp.BasicAuth(EXOTEL_API_KEY, EXOTEL_API_TOKEN)

        # Get proxy configuration
        proxy_url = get_proxy_config()

        logger.info(f"Downloading Exotel recording from: {recording_url}")

        async with aiohttp.ClientSession() as session:
            async with session.get(
                recording_url, auth=auth, proxy=proxy_url
            ) as response:
                if response.status != 200:
                    logger.error(
                        f"Failed to download Exotel recording. Status: {response.status}"
                    )
                    return None

                # Read the recording into memory
                audio_data = await response.read()
                audio_file = BytesIO(audio_data)

        logger.info(
            f"Successfully downloaded Exotel recording for call: {call_sid} ({len(audio_data)} bytes)"
        )
        return audio_file

    except Exception as e:
        logger.error(f"Error downloading Exotel recording: {e}", exc_info=True)
        return None
