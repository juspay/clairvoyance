"""
AWS Utils - Generalized AWS client initialization
Provides reusable AWS client creation for various AWS services
"""

from typing import Optional

import boto3

from app.core import config
from app.core.logger import logger


def get_aws_client(service_name: str) -> Optional[any]:
    """
    Initializes and returns an AWS client for the specified service.

    Args:
        service_name (str): The AWS service name (e.g., 'kms', 's3', 'ec2', 'lambda')

    Returns:
        Optional[any]: The AWS client instance or None if initialization fails
    """
    try:
        # Check if AWS credentials are available
        if not config.AWS_ACCESS_KEY_ID or not config.AWS_SECRET_ACCESS_KEY:
            logger.error(f"AWS credentials missing for {service_name} client")
            return None

        # Create the AWS client
        client = boto3.client(
            service_name,
            aws_access_key_id=config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
            region_name=config.AWS_REGION,
        )

        logger.info(
            f"AWS {service_name} client initialized for region: {config.AWS_REGION}"
        )
        return client

    except Exception as e:
        logger.error(f"Failed to initialize AWS {service_name} client: {e}")
        return None
