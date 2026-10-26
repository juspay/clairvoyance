"""
AWS KMS decryption (the CLOUD_ENVIRONMENT=AWS branch of app/services/kms.py).
The provider-neutral entry point, dev/beta short-circuits and provider dispatch
live in app/services/kms.py; this module only talks to AWS.
"""

import base64
from typing import Optional

from app.core.logger import logger
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


async def decrypt_with_aws_kms(encrypted_string: str) -> Optional[str]:
    """
    Decrypt a base64 AWS KMS ciphertext, retrying once.
    Returns None when the client is unavailable or both attempts fail.
    """
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
