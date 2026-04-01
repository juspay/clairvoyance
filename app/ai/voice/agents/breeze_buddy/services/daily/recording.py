"""
Daily recording download functionality
"""

from io import BytesIO
from typing import Optional

from app.core.config.static import (
    BREEZE_BUDDY_DAILY_API_KEY,
    BREEZE_BUDDY_DAILY_API_URL,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session


async def download_call_recording(
    room_name: str,
) -> Optional[BytesIO]:
    """
    Download a recording from Daily directly into memory.

    Flow:
    1. GET /recordings?room_name=<room_name> to find the recording
    2. GET /recordings/<id>/access-link to get a temporary download URL
    3. Download and return audio bytes

    Args:
        room_name (str): The Daily room name to fetch recording for

    Returns:
        Optional[BytesIO]: In-memory file object containing the recording, or None if download failed
    """
    try:
        async with create_aiohttp_session() as session:
            headers = {"Authorization": f"Bearer {BREEZE_BUDDY_DAILY_API_KEY}"}

            # Step 1: List recordings for this room
            list_url = f"{BREEZE_BUDDY_DAILY_API_URL}/recordings?room_name={room_name}"
            async with session.get(list_url, headers=headers) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(
                        f"Failed to list Daily recordings for room {room_name}: {error_text}"
                    )
                    return None
                data = await resp.json()

            recordings = data.get("data", [])
            if not recordings:
                logger.warning(f"No recordings found for room: {room_name}")
                return None

            # Pick the latest recording (first in list, sorted by created_at desc)
            recording_id = recordings[0]["id"]

            # Step 2: Get temporary access link
            access_url = (
                f"{BREEZE_BUDDY_DAILY_API_URL}/recordings/{recording_id}/access-link"
            )
            async with session.get(access_url, headers=headers) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(
                        f"Failed to get Daily access link for recording {recording_id}: {error_text}"
                    )
                    return None
                access_data = await resp.json()

            download_link = access_data.get("download_link")
            if not download_link:
                logger.error(
                    f"No download_link in Daily access response for recording {recording_id}"
                )
                return None

            # Step 3: Download the recording
            async with session.get(download_link) as resp:
                if resp.status != 200:
                    logger.error(
                        f"Failed to download Daily recording {recording_id}: status={resp.status}"
                    )
                    return None

                audio_data = await resp.read()
                audio_file = BytesIO(audio_data)

        logger.info(
            f"Successfully downloaded Daily recording for room: {room_name} ({len(audio_data)} bytes)"
        )
        return audio_file

    except Exception as e:
        logger.error(
            f"Error downloading Daily recording for room {room_name}: {e}",
            exc_info=True,
        )
        return None
