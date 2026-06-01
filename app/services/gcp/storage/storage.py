"""
GCS Storage - Google Cloud Storage operations
Simplified module for uploading audio files to GCS
"""

from typing import BinaryIO, Optional

from app.core.config.static import GCS_BUCKET
from app.core.logger import logger

from .client import get_gcs_bucket


class GCSStorage:
    """
    Google Cloud Storage utility class for audio file uploads.
    Provides methods to upload audio files to GCS buckets.
    """

    def __init__(self):
        """
        Initialize GCS Storage with a specific bucket.
        """
        self.bucket = get_gcs_bucket(GCS_BUCKET)

    def upload_file_object(
        self,
        file_obj: BinaryIO,
        destination_path: str,
        content_type: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> bool:
        """
        Upload an audio file-like object to GCS bucket.

        Args:
            file_obj (BinaryIO): File-like object to upload
            destination_path (str): The destination path/name in GCS bucket
            content_type (Optional[str]): MIME type of the file (e.g., 'audio/wav', 'audio/mp3')
            metadata (Optional[dict]): Additional metadata to attach to the file

        Returns:
            bool: True if upload was successful, False otherwise
        """
        try:
            if not self.bucket:
                logger.error("GCS bucket not initialized")
                return False

            # Create blob object
            blob = self.bucket.blob(destination_path)

            # Set content type if provided
            if content_type:
                blob.content_type = content_type

            # Set metadata if provided
            if metadata:
                blob.metadata = metadata

            # Upload the file object
            file_obj.seek(0)  # Reset to beginning of file
            blob.upload_from_file(file_obj)

            logger.info(
                f"File object uploaded successfully to gs://{GCS_BUCKET}/{destination_path}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to upload file object to GCS: {e}", exc_info=True)
            return False


def upload_file_to_gcs(
    file_obj: BinaryIO,
    destination_path: str,
    content_type: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> Optional[str]:
    """
    Simplified utility function to upload a file object to GCS.

    Args:
        file_obj (BinaryIO): File-like object to upload
        destination_path (str): The destination path/name in GCS bucket (e.g., "recordings/twilio/call123.wav")
        content_type (Optional[str]): MIME type of the file (e.g., 'audio/wav')
        metadata (Optional[dict]): Additional metadata to attach to the file

    Returns:
        Optional[str]: The GCS URL if successful, None otherwise
    """
    try:
        gcs_storage = GCSStorage()
        success = gcs_storage.upload_file_object(
            file_obj=file_obj,
            destination_path=destination_path,
            content_type=content_type,
            metadata=metadata,
        )

        if success:
            if GCS_BUCKET == "atoms-sdk":
                gcs_url = f"https://sdk.beta.breezesdk.store/{destination_path}"
                return gcs_url
        return None

    except Exception as e:
        logger.error(f"Error in upload_file_to_gcs: {e}", exc_info=True)
        return None
