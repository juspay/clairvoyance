"""
Plivo recording functionality
"""

from io import BytesIO
from typing import Optional

import aiohttp
import plivo

from app.core.config.static import (
    APP_BASE_URL,
    PLIVO_AUTH_ID,
    PLIVO_AUTH_TOKEN,
    PLIVO_RECORDING_TIME_LIMIT,
)
from app.core.logger import logger
from app.core.security.ssrf import ssrf_safe_request
from app.core.transport.http_client import get_proxy_config

# Only ever send Plivo BasicAuth to Plivo's own hosts.
_PLIVO_HOST_SUFFIXES = ("plivo.com",)


def start_call_recording(call_uuid: str) -> bool:
    """
    Start recording an active call via Plivo API.

    Args:
        call_uuid: The Plivo call UUID

    Returns:
        bool: True if recording started successfully, False otherwise
    """
    try:
        client = plivo.RestClient(PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN)

        logger.info(f"Starting recording for Plivo call: {call_uuid}")

        # Start recording the call with callback URL
        callback_url = f"{APP_BASE_URL}/agent/voice/breeze-buddy/plivo/callback/details"
        response = client.calls.record(
            call_uuid=call_uuid,
            callback_url=callback_url,
            callback_method="POST",
            time_limit=PLIVO_RECORDING_TIME_LIMIT,
        )

        logger.info(f"Plivo recording started successfully: {response}")
        return True

    except Exception as e:
        logger.error(f"Error starting Plivo recording: {e}", exc_info=True)
        return False


async def download_call_recording(
    recording_url: str, call_sid: str
) -> Optional[BytesIO]:
    """
    Download a recording from Plivo directly into memory.

    Args:
        recording_url (str): The URL of the recording to download
        call_sid (str): The call SID for logging purposes

    Returns:
        Optional[BytesIO]: In-memory file object containing the recording, or None if download failed
    """
    try:
        # Plivo recordings require basic authentication
        auth = aiohttp.BasicAuth(PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN)

        # Get proxy configuration
        proxy_url = get_proxy_config()

        logger.info(f"Downloading Plivo recording from: {recording_url}")

        # SSRF: never send Plivo credentials to a non-Plivo or internal host,
        # even if a forged webhook supplied the URL (PT-05).
        async with aiohttp.ClientSession() as session:
            async with ssrf_safe_request(
                session,
                "GET",
                recording_url,
                auth=auth,
                allowed_host_suffixes=_PLIVO_HOST_SUFFIXES,
                proxy=proxy_url,
            ) as response:
                if response.status != 200:
                    logger.error(
                        f"Failed to download Plivo recording. Status: {response.status}"
                    )
                    return None

                # Read the recording into memory
                audio_data = await response.read()
                audio_file = BytesIO(audio_data)

        logger.info(
            f"Successfully downloaded Plivo recording for call: {call_sid} ({len(audio_data)} bytes)"
        )
        return audio_file

    except Exception as e:
        logger.error(f"Error downloading Plivo recording: {e}", exc_info=True)
        return None
