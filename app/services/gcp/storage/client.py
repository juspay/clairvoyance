"""
GCS Client - Google Cloud Storage client initialization
Provides reusable GCS client creation with service account authentication
"""

import json
from typing import Optional

from google.cloud import storage
from google.oauth2 import service_account

from app.core.config.static import GCS_CREDENTIALS_JSON
from app.core.logger import logger


def get_gcs_client() -> Optional[storage.Client]:
    """
    Initializes and returns a Google Cloud Storage client using service account credentials.

    Returns:
        Optional[storage.Client]: The GCS client instance or None if initialization fails
    """
    try:
        # Check if GCS credentials are available
        if not GCS_CREDENTIALS_JSON:
            logger.error("GCS_CREDENTIALS_JSON environment variable is not set")
            return None

        # Parse the JSON credentials
        try:
            credentials_dict = json.loads(GCS_CREDENTIALS_JSON)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse GCS credentials JSON: {e}")
            return None

        # Create credentials object from service account info
        credentials = service_account.Credentials.from_service_account_info(
            credentials_dict
        )

        # Create and return the GCS client
        client = storage.Client(
            credentials=credentials, project=credentials_dict.get("project_id")
        )

        logger.info(
            f"GCS client initialized for project: {credentials_dict.get('project_id')}"
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
