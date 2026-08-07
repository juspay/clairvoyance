"""
GCS Client - Google Cloud Storage client initialization.
"""

from typing import Optional

from google.cloud import storage

from app.core.config.static import GCS_CREDENTIALS_JSON
from app.core.logger import logger
from app.services.gcp.credentials import get_google_credentials


def get_gcs_client() -> Optional[storage.Client]:
    """
    Return a GCS client using ADC with legacy JSON credentials as fallback.

    Returns:
        Optional[storage.Client]: The GCS client instance or None if initialization fails
    """
    try:
        auth = get_google_credentials(
            credentials_json=GCS_CREDENTIALS_JSON,
            service_name="GCS",
        )

        # Create and return the GCS client
        client = storage.Client(credentials=auth.credentials, project=auth.project_id)

        logger.info(
            f"GCS client initialized using {auth.source}"
            f"{f' for project: {auth.project_id}' if auth.project_id else ''}"
        )
        return client

    except Exception as e:
        logger.error(f"Failed to initialize GCS client: {e}")
        return None


def get_gcs_bucket(bucket_name: str) -> Optional[storage.Bucket]:
    """
    Gets a GCS bucket object.

    Args:
        bucket_name (str): The name of the GCS bucket

    Returns:
        Optional[storage.Bucket]: The bucket object or None if initialization fails
    """
    try:
        client = get_gcs_client()
        if not client:
            return None

        bucket = client.bucket(bucket_name)
        logger.info(f"GCS bucket '{bucket_name}' accessed successfully")
        return bucket

    except Exception as e:
        logger.error(f"Failed to access GCS bucket '{bucket_name}': {e}")
        return None
