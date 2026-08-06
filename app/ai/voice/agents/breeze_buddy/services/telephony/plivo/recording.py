"""
Plivo recording functionality
"""

import asyncio
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
from app.core.transport.http_client import get_proxy_config


def _start_call_recording_blocking(call_uuid: str) -> bool:
    """
    Blocking implementation — DO NOT call this from an ``async def``.

    ``plivo.RestClient`` is a synchronous HTTP client. It has no ``await``
    and cannot have one; while it waits ~166ms for Plivo's API, a single-
    worker uvicorn process is completely frozen — no other call, callback,
    or background task can run. Reach it only via ``start_call_recording``.
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
        # logger.opt(exception=...) rather than exc_info=: loguru has no
        # exc_info kwarg — it would be consumed as a str.format argument,
        # dropping the traceback and raising KeyError whenever the Plivo
        # error text contains braces (a JSON body, for instance).
        logger.opt(exception=e).error(f"Error starting Plivo recording: {e}")
        return False


async def start_call_recording(call_uuid: str) -> bool:
    """
    Start recording an active call via Plivo API, off the event loop.

    Args:
        call_uuid: The Plivo call UUID

    Returns:
        bool: True if recording started successfully, False otherwise
    """
    return await asyncio.to_thread(_start_call_recording_blocking, call_uuid)


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

        async with aiohttp.ClientSession() as session:
            async with session.get(
                recording_url, auth=auth, proxy=proxy_url
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
        logger.opt(exception=e).error(f"Error downloading Plivo recording: {e}")
        return None
