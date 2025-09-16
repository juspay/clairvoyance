"""
KMS Service for conditional decryption based on cloud provider
Only performs decryption when CLOUD_ENVIRONMENT=AWS, otherwise returns the original string
"""

import base64
from typing import Optional
from app.core.logger import logger
from app.core import config
from app.services.aws.utils import get_aws_client


def _perform_decryption(kms_client, encrypted_string: str) -> Optional[str]:
    """Helper function to perform the actual decryption."""
    try:
        ciphertext = base64.b64decode(encrypted_string)
        response = kms_client.decrypt(CiphertextBlob=ciphertext)

        if not response.get("Plaintext"):
            logger.error("No plaintext in AWS KMS response")
            return None

        decrypted = response["Plaintext"].decode("utf-8")
        logger.info("AWS KMS decryption successful")
        return decrypted
    except Exception as e:
        logger.warning(f"AWS KMS decryption failed: {e}")
        return None


async def decrypt_kms(encrypted_string: str) -> Optional[str]:
    """
    Decrypt KMS encrypted string with built-in retry logic.
    Only performs decryption when CLOUD_ENVIRONMENT=AWS.
    """
    logger.debug(
        f"decrypt_kms called with string length: {len(encrypted_string) if encrypted_string else 0}"
    )

    if not encrypted_string:
        logger.warning("Empty encrypted string provided")
        return None

    if config.ENVIRONMENT.lower() == "dev":
        logger.info("Skipping KMS decryption in dev environment")
        return encrypted_string

    if config.ENVIRONMENT.lower() == "beta" and config.SKIP_KMS_DECRYPT:
        logger.info("Skipping KMS decryption in beta with skip flag")
        return encrypted_string

    if config.CLOUD_ENVIRONMENT != "AWS":
        logger.info(f"Skipping KMS decryption - provider is {config.CLOUD_ENVIRONMENT}")
        return encrypted_string

    kms_client = get_aws_client("kms")
    if not kms_client:
        logger.error("AWS KMS client not available")
        return None

    for attempt in range(2):
        decrypted_result = _perform_decryption(kms_client, encrypted_string)
        if decrypted_result is not None:
            return decrypted_result
        if attempt == 0:
            logger.info("Retrying AWS KMS decryption")

    logger.error("AWS KMS decryption failed after all attempts")
    return None
