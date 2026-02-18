"""
GCP KMS Service for encrypting and decrypting credential values.

Uses Google Cloud Key Management Service for symmetric encryption.
Gracefully falls back to plaintext when KMS is not configured (dev/local environments).
"""

import base64
import json
from typing import Optional

from app.core.config.static import (
    CLOUD_ENVIRONMENT,
    ENVIRONMENT,
    GCP_KMS_CREDENTIALS_JSON,
    GCP_KMS_KEY_NAME,
    GCP_KMS_KEYRING,
    GCP_KMS_LOCATION,
    GCP_KMS_PROJECT_ID,
    SKIP_KMS_DECRYPT,
)
from app.core.logger import logger

# Lazy-loaded KMS client
_kms_client = None


def _is_kms_configured() -> bool:
    """Check if GCP KMS is properly configured."""
    return bool(
        GCP_KMS_PROJECT_ID
        and GCP_KMS_LOCATION
        and GCP_KMS_KEYRING
        and GCP_KMS_KEY_NAME
    )


def _get_kms_client():
    """Get or create a GCP KMS client (lazy initialization)."""
    global _kms_client
    if _kms_client is not None:
        return _kms_client

    try:
        from google.cloud import kms
        from google.oauth2 import service_account

        if GCP_KMS_CREDENTIALS_JSON:
            credentials_dict = json.loads(GCP_KMS_CREDENTIALS_JSON)
            credentials = service_account.Credentials.from_service_account_info(
                credentials_dict
            )
            _kms_client = kms.KeyManagementServiceClient(credentials=credentials)
        else:
            # Use Application Default Credentials (ADC)
            _kms_client = kms.KeyManagementServiceClient()

        logger.info("GCP KMS client initialized successfully")
        return _kms_client

    except Exception as e:
        logger.error(f"Failed to initialize GCP KMS client: {e}")
        return None


def _get_key_name() -> str:
    """Build the full KMS crypto key resource name."""
    return (
        f"projects/{GCP_KMS_PROJECT_ID}/"
        f"locations/{GCP_KMS_LOCATION}/"
        f"keyRings/{GCP_KMS_KEYRING}/"
        f"cryptoKeys/{GCP_KMS_KEY_NAME}"
    )


def encrypt_value(plaintext: str) -> Optional[str]:
    """
    Encrypt a plaintext string using GCP KMS.

    Returns base64-encoded ciphertext, or None if encryption fails.
    When KMS is not configured, returns None (caller should store plaintext).
    """
    if not _is_kms_configured():
        logger.debug("GCP KMS not configured, skipping encryption")
        return None

    if ENVIRONMENT.lower() == "dev":
        logger.debug("Skipping KMS encryption in dev environment")
        return None

    if CLOUD_ENVIRONMENT != "GCP":
        logger.debug(f"Skipping GCP KMS encryption - provider is {CLOUD_ENVIRONMENT}")
        return None

    client = _get_kms_client()
    if not client:
        logger.warning("GCP KMS client not available, cannot encrypt")
        return None

    try:
        key_name = _get_key_name()
        plaintext_bytes = plaintext.encode("utf-8")

        response = client.encrypt(
            request={"name": key_name, "plaintext": plaintext_bytes}
        )

        ciphertext_b64 = base64.b64encode(response.ciphertext).decode("utf-8")
        logger.debug("GCP KMS encryption successful")
        return ciphertext_b64

    except Exception as e:
        logger.error(f"GCP KMS encryption failed: {e}")
        return None


def decrypt_value(ciphertext_b64: str) -> Optional[str]:
    """
    Decrypt a base64-encoded ciphertext using GCP KMS.

    Returns decrypted plaintext string, or None if decryption fails.
    """
    if not _is_kms_configured():
        logger.debug("GCP KMS not configured, skipping decryption")
        return None

    if ENVIRONMENT.lower() == "dev":
        logger.debug("Skipping KMS decryption in dev environment")
        return None

    if ENVIRONMENT.lower() == "beta" and SKIP_KMS_DECRYPT:
        logger.debug("Skipping KMS decryption in beta with skip flag")
        return None

    if CLOUD_ENVIRONMENT != "GCP":
        logger.debug(f"Skipping GCP KMS decryption - provider is {CLOUD_ENVIRONMENT}")
        return None

    client = _get_kms_client()
    if not client:
        logger.warning("GCP KMS client not available, cannot decrypt")
        return None

    try:
        key_name = _get_key_name()
        ciphertext = base64.b64decode(ciphertext_b64)

        response = client.decrypt(
            request={"name": key_name, "ciphertext": ciphertext}
        )

        plaintext = response.plaintext.decode("utf-8")
        logger.debug("GCP KMS decryption successful")
        return plaintext

    except Exception as e:
        logger.error(f"GCP KMS decryption failed: {e}")
        return None


def encrypt_credential(value_dict: dict) -> tuple[str, bool]:
    """
    Encrypt a credential value dictionary for storage.

    Returns:
        (stored_value, is_encrypted) tuple.
        - If KMS is available: (base64_ciphertext, True)
        - If KMS is not available: (json_string, False)
    """
    json_str = json.dumps(value_dict)
    encrypted = encrypt_value(json_str)

    if encrypted is not None:
        return encrypted, True

    # Fallback: store as plain JSON string
    return json_str, False


def decrypt_credential(stored_value: str, is_encrypted: bool) -> Optional[dict]:
    """
    Decrypt a stored credential value back to a dictionary.

    Args:
        stored_value: The stored value (either ciphertext or JSON string)
        is_encrypted: Whether the value was encrypted with KMS

    Returns:
        Decrypted dictionary, or None if decryption fails
    """
    try:
        if is_encrypted:
            decrypted = decrypt_value(stored_value)
            if decrypted is None:
                logger.error("Failed to decrypt credential value via KMS")
                return None
            return json.loads(decrypted)
        else:
            return json.loads(stored_value)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse credential value as JSON: {e}")
        return None
