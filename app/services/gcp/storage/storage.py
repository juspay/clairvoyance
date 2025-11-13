"""
GCS Storage - Google Cloud Storage operations
Simplified module for uploading audio files to GCS
"""

from pathlib import Path
from typing import BinaryIO, Optional, Union

from app.core.logger import logger

from .client import get_gcs_bucket


class GCSStorage:
    """
    Google Cloud Storage utility class for audio file uploads.
    Provides methods to upload audio files to GCS buckets.
    """

    def __init__(self, bucket_name: str):
        """
        Initialize GCS Storage with a specific bucket.

        Args:
            bucket_name (str): The name of the GCS bucket to use
        """
        self.bucket_name = bucket_name
        self.bucket = get_gcs_bucket(bucket_name)

    def upload_file(
        self,
        source_file_path: Union[str, Path],
        destination_blob_name: str,
        content_type: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> bool:
        """
        Upload an audio file to GCS bucket.

        Args:
            source_file_path (Union[str, Path]): Local path to the audio file to upload
            destination_blob_name (str): The destination path/name in GCS bucket
            content_type (Optional[str]): MIME type of the file (e.g., 'audio/wav', 'audio/mp3')
            metadata (Optional[dict]): Additional metadata to attach to the file

        Returns:
            bool: True if upload was successful, False otherwise
        """
        try:
            if not self.bucket:
                logger.error("GCS bucket not initialized")
                return False

            # Convert to Path object for easier handling
            source_path = Path(source_file_path)

            if not source_path.exists():
                logger.error(f"Source file does not exist: {source_file_path}")
                return False

            # Create blob object
            blob = self.bucket.blob(destination_blob_name)

            # Set content type if provided
            if content_type:
                blob.content_type = content_type

            # Set metadata if provided
            if metadata:
                blob.metadata = metadata

            # Upload the file
            blob.upload_from_filename(str(source_path))

            logger.info(
                f"File uploaded successfully: {source_file_path} -> gs://{self.bucket_name}/{destination_blob_name}"
            )
            return True

        except Exception as e:
            logger.error(
                f"Failed to upload file {source_file_path} to GCS: {e}", exc_info=True
            )
            return False

    def upload_file_object(
        self,
        file_obj: BinaryIO,
        destination_blob_name: str,
        content_type: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> bool:
        """
        Upload an audio file-like object to GCS bucket.

        Args:
            file_obj (BinaryIO): File-like object to upload
            destination_blob_name (str): The destination path/name in GCS bucket
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
            blob = self.bucket.blob(destination_blob_name)

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
                f"File object uploaded successfully to gs://{self.bucket_name}/{destination_blob_name}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to upload file object to GCS: {e}", exc_info=True)
            return False
