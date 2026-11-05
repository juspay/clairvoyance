"""
Google Cloud KMS decryption (the CLOUD_ENVIRONMENT=GCP branch of
app/services/kms.py).

Active only when GCP_KMS_KEY_NAME is set; app/services/kms.py never calls
this module otherwise, so a GCP deployment whose POSTGRES_PASSWORD is still
plaintext keeps working unchanged. Credentials come from
GCP_KMS_CREDENTIALS_JSON (service-account JSON, like GCS_CREDENTIALS_JSON) or,
when that is empty, Application Default Credentials (Workload Identity).
"""

import base64
import json
from typing import Optional

import google_crc32c
from google.cloud import kms
from google.oauth2 import service_account

from app.core.config.static import GCP_KMS_CREDENTIALS_JSON, GCP_KMS_KEY_NAME
from app.core.logger import logger


def get_gcp_kms_client() -> Optional[kms.KeyManagementServiceAsyncClient]:
    """
    Build the KMS client: explicit service-account JSON when
    GCP_KMS_CREDENTIALS_JSON is set, Application Default Credentials otherwise.
    Returns None (never raises) when the client cannot be built.
    """
    try:
        if not GCP_KMS_CREDENTIALS_JSON:
            logger.info(
                "GCP_KMS_CREDENTIALS_JSON not set; using Application Default Credentials"
            )
            return kms.KeyManagementServiceAsyncClient()

        try:
            credentials_dict = json.loads(GCP_KMS_CREDENTIALS_JSON)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse GCP KMS credentials JSON: {e}")
            return None

        credentials = service_account.Credentials.from_service_account_info(
            credentials_dict
        )
        client = kms.KeyManagementServiceAsyncClient(credentials=credentials)
        logger.info(
            f"GCP KMS client initialized for project: {credentials_dict.get('project_id')}"
        )
        return client
    except Exception as e:
        logger.error(f"Failed to initialize GCP KMS client: {e}")
        return None


def _crc32c(data: bytes) -> int:
    """CRC32C as Google KMS carries it: an unsigned 32-bit int."""
    return int(google_crc32c.value(data))


async def _perform_decryption(
    kms_client: kms.KeyManagementServiceAsyncClient, encrypted_string: str
) -> Optional[str]:
    """One decrypt attempt; every google/base64 failure becomes None here.

    Both directions carry a CRC32C (Google's own recommendation): the request
    one lets KMS refuse a ciphertext corrupted in transit, and the response
    one is checked here so a plaintext corrupted on the way back is never
    handed to the caller as a password.
    """
    try:
        ciphertext = base64.b64decode(encrypted_string)
        response = await kms_client.decrypt(
            request={
                "name": GCP_KMS_KEY_NAME,
                "ciphertext": ciphertext,
                "ciphertext_crc32c": _crc32c(ciphertext),
            }
        )

        if not response.plaintext:
            logger.error("No plaintext in GCP KMS response")
            return None

        if response.plaintext_crc32c != _crc32c(response.plaintext):
            logger.error("GCP KMS response failed the CRC32C integrity check")
            return None

        decrypted = response.plaintext.decode("utf-8")
        logger.info("GCP KMS decryption successful")
        return decrypted
    except Exception as e:
        logger.warning(f"GCP KMS decryption failed: {e}")
        return None


async def decrypt_with_gcp_kms(encrypted_string: str) -> Optional[str]:
    """
    Decrypt a base64 Google Cloud KMS ciphertext with GCP_KMS_KEY_NAME,
    retrying once. Returns None (fail closed) when the key name is missing,
    the client cannot be built, or both attempts fail.
    """
    if not GCP_KMS_KEY_NAME:
        logger.error("GCP_KMS_KEY_NAME not set; cannot decrypt with GCP KMS")
        return None

    kms_client = get_gcp_kms_client()
    if kms_client is None:
        logger.error("GCP KMS client not available")
        return None

    decrypted_result: Optional[str] = None
    try:
        async with kms_client:
            for attempt in range(2):
                decrypted_result = await _perform_decryption(
                    kms_client, encrypted_string
                )
                if decrypted_result is not None:
                    break
                if attempt == 0:
                    logger.info("Retrying GCP KMS decryption")
    except Exception as e:
        # _perform_decryption never raises, so only closing the gRPC transport
        # can land here; a plaintext already obtained is still returned.
        logger.warning(f"GCP KMS client teardown failed: {e}")

    if decrypted_result is not None:
        return decrypted_result

    logger.error("GCP KMS decryption failed after all attempts")
    return None
