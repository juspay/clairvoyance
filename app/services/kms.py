"""
KMS decryption, provider-neutral. CLOUD_ENVIRONMENT picks the backend:

    AWS           -> app/services/aws/kms.py (boto3)
    GCP           -> app/services/gcp/kms.py, ONLY when GCP_KMS_KEY_NAME is set;
                     unset keeps today's passthrough so GCP deployments whose
                     POSTGRES_PASSWORD is plaintext are unaffected
    anything else -> passthrough (the input string is returned unchanged)

Dev, and beta with SKIP_KMS_DECRYPT, short-circuit to passthrough before any
provider is consulted.
"""

from typing import Optional

from app.core.config.static import (
    CLOUD_ENVIRONMENT,
    ENVIRONMENT,
    GCP_KMS_KEY_NAME,
    SKIP_KMS_DECRYPT,
)
from app.core.logger import logger
from app.services.aws.kms import decrypt_with_aws_kms
from app.services.gcp.kms import decrypt_with_gcp_kms


async def decrypt_kms(encrypted_string: str) -> Optional[str]:
    """
    Decrypt a KMS-encrypted string with the provider named by
    CLOUD_ENVIRONMENT. Returns the plaintext, the input unchanged when no
    decryption applies, or None when decryption was attempted and failed
    (each provider retries once before giving up).
    """
    logger.debug(
        f"decrypt_kms called with string length: {len(encrypted_string) if encrypted_string else 0}"
    )

    if not encrypted_string:
        logger.warning("Empty encrypted string provided")
        return None

    if ENVIRONMENT.lower() == "dev":
        logger.info("Skipping KMS decryption in dev environment")
        return encrypted_string

    if ENVIRONMENT.lower() == "beta" and SKIP_KMS_DECRYPT:
        logger.info("Skipping KMS decryption in beta with skip flag")
        return encrypted_string

    if CLOUD_ENVIRONMENT == "AWS":
        return await decrypt_with_aws_kms(encrypted_string)

    if CLOUD_ENVIRONMENT == "GCP" and GCP_KMS_KEY_NAME:
        return await decrypt_with_gcp_kms(encrypted_string)

    logger.info(f"Skipping KMS decryption - provider is {CLOUD_ENVIRONMENT}")
    return encrypted_string
