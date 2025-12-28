"""
Call recording management utilities.
"""

from app.ai.voice.agents.breeze_buddy.services.telephony.exotel.recording import (
    download_call_recording as download_call_recording_exotel,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.twilio.recording import (
    download_call_recording as download_call_recording_twilio,
)
from app.core.config.static import UPLOAD_BREEZE_BUDDY_CALL_RECORDINGS_TO_CLOUD
from app.core.logger import logger
from app.database.accessor import get_lead_by_call_id, update_lead_call_recording_url
from app.services.gcp.storage.storage import upload_file_to_gcs


async def update_call_recording(
    call_id: str, provider_recording_url: str, provider: str
):
    """
    Processes the call recording based on UPLOAD_BREEZE_BUDDY_CALL_RECORDINGS_TO_CLOUD flag.

    If flag is enabled:
        - Downloads the call recording from the provider
        - Uploads it to GCS
        - Updates the lead with the GCS URL

    If flag is disabled:
        - Stores only the provider recording URL in the database

    Args:
        call_id: The call SID
        provider_recording_url: The URL of the recording from the provider
        provider: The provider name ('twilio' or 'exotel')
    """
    logger.info(
        f"Processing call recording for call_id: {call_id} from provider: {provider}"
    )
    lead = await get_lead_by_call_id(call_id)
    provider = provider.lower()
    if not lead:
        logger.error(f"Could not find lead for call_id: {call_id}")
        return

    try:
        # If cloud upload is disabled, just store the provider URL in DB
        if not UPLOAD_BREEZE_BUDDY_CALL_RECORDINGS_TO_CLOUD:
            logger.info(
                f"Cloud upload disabled. Storing provider recording URL in DB for call_id: {call_id}"
            )
            await update_lead_call_recording_url(call_id, provider_recording_url)
            return

        # Cloud upload is enabled - download from provider and upload to GCS
        if provider == "twilio":
            audio_file = await download_call_recording_twilio(
                provider_recording_url, call_id
            )
        elif provider == "exotel":
            audio_file = await download_call_recording_exotel(
                provider_recording_url, call_id
            )
        else:
            logger.error(f"Unsupported provider: {provider}")
            return

        if not audio_file:
            logger.error(f"Failed to download recording for call_id: {call_id}")
            await update_lead_call_recording_url(call_id, provider_recording_url)
            return

        if provider == "twilio":
            content_type = "audio/wav"
            file_extension = "wav"
        else:  # exotel
            content_type = "audio/mp3"
            file_extension = "mp3"

        gcs_url = upload_file_to_gcs(
            file_obj=audio_file,
            destination_path=f"breeze-buddy/recordings/{call_id}.{file_extension}",
            content_type=content_type,
            metadata={
                "call_id": call_id,
                "original_url": provider_recording_url,
            },
        )

        if gcs_url:
            logger.info(f"Successfully uploaded recording to GCS: {gcs_url}")
            await update_lead_call_recording_url(call_id, gcs_url)
        else:
            logger.error(f"Failed to upload recording to GCS for call_id: {call_id}")
            await update_lead_call_recording_url(call_id, provider_recording_url)

    except Exception as e:
        logger.error(
            f"Error processing call recording for call_id {call_id}: {e}",
            exc_info=True,
        )
