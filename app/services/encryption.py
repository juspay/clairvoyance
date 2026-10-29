"""
AES-256-GCM encryption for credential values at rest.

Uses a symmetric key from the CREDENTIAL_ENCRYPTION_KEY env var. Writes FAIL
CLOSED: with no usable key, or when encryption itself fails, the credential is
refused (CredentialEncryptionError), never stored readable. Rows written as
plain JSON before this rule (``is_encrypted=False``) still decrypt.

Key generation (run once, store in env):
    python -c "import os, base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
"""

import base64
import json
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config.static import CREDENTIAL_ENCRYPTION_KEY
from app.core.logger import logger

# AES-256-GCM nonce size (96 bits recommended by NIST)
_NONCE_SIZE = 12


class CredentialEncryptionError(Exception):
    """A credential could not be encrypted — no usable key, or AES-GCM
    failed. The write is refused rather than stored as plain JSON."""


def _get_key_bytes() -> Optional[bytes]:
    """Decode the base64-encoded 256-bit encryption key."""
    if not CREDENTIAL_ENCRYPTION_KEY:
        return None
    try:
        key = base64.urlsafe_b64decode(CREDENTIAL_ENCRYPTION_KEY)
        if len(key) != 32:
            logger.error(
                f"CREDENTIAL_ENCRYPTION_KEY must be 32 bytes (got {len(key)}). "
                'Generate with: python -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"'
            )
            return None
        return key
    except Exception as e:
        logger.error(f"Failed to decode CREDENTIAL_ENCRYPTION_KEY: {e}")
        return None


def credential_encryption_ready() -> bool:
    """True when CREDENTIAL_ENCRYPTION_KEY decodes to a usable 32-byte key —
    the startup check; without it every credential write is refused."""
    return _get_key_bytes() is not None


def encrypt_value(plaintext: str) -> Optional[str]:
    """
    Encrypt a plaintext string using AES-256-GCM.

    Returns base64-encoded (nonce + ciphertext + tag), or None if key is not configured.
    """
    key = _get_key_bytes()
    if key is None:
        return None

    try:
        nonce = os.urandom(_NONCE_SIZE)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)

        # Store as: nonce (12 bytes) + ciphertext+tag
        encrypted_blob = nonce + ciphertext
        return base64.urlsafe_b64encode(encrypted_blob).decode("utf-8")

    except Exception as e:
        logger.error(f"AES-256-GCM encryption failed: {e}")
        return None


def decrypt_value(encrypted_b64: str) -> Optional[str]:
    """
    Decrypt a base64-encoded AES-256-GCM value.

    Expects format: base64(nonce + ciphertext + tag).
    """
    key = _get_key_bytes()
    if key is None:
        logger.error("Cannot decrypt: CREDENTIAL_ENCRYPTION_KEY not set")
        return None

    try:
        encrypted_blob = base64.urlsafe_b64decode(encrypted_b64)
        if len(encrypted_blob) < _NONCE_SIZE + 16:  # nonce + minimum tag
            logger.error("Encrypted data too short")
            return None

        nonce = encrypted_blob[:_NONCE_SIZE]
        ciphertext = encrypted_blob[_NONCE_SIZE:]

        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return plaintext.decode("utf-8")

    except Exception as e:
        logger.error(f"AES-256-GCM decryption failed: {e}")
        return None


def encrypt_credential(value_dict: dict) -> str:
    """
    Encrypt a credential value dictionary for storage.

    Returns the base64 ciphertext. Raises CredentialEncryptionError when the
    key is missing or invalid, or encryption fails — the value is never
    returned in a storable plain form (fail closed).
    """
    encrypted = encrypt_value(json.dumps(value_dict))
    if encrypted is None:
        logger.error(
            "Refusing to store a credential: CREDENTIAL_ENCRYPTION_KEY is missing "
            "or invalid, or encryption failed (would otherwise be stored as plain text)"
        )
        raise CredentialEncryptionError("credential encryption is not available")
    return encrypted


def decrypt_credential(stored_value: str, is_encrypted: bool) -> Optional[dict]:
    """
    Decrypt a stored credential value back to a dictionary.

    Args:
        stored_value: The stored value (either ciphertext or JSON string)
        is_encrypted: Whether the value was encrypted

    Returns:
        Decrypted dictionary, or None if decryption fails
    """
    try:
        if is_encrypted:
            decrypted = decrypt_value(stored_value)
            if decrypted is None:
                logger.error("Failed to decrypt credential value")
                return None
            return json.loads(decrypted)
        else:
            return json.loads(stored_value)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse credential value as JSON: {e}")
        return None
