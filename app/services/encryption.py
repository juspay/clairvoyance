"""
AES-256-GCM encryption for credential values at rest.

Uses a symmetric key from the CREDENTIAL_ENCRYPTION_KEY env var. Without
one, storing a credential RAISES — no fallback, no opt-out flag. A missing
key is a misconfiguration, and the value being written is a live provider
secret (a WhatsApp system-user token is full API access to a merchant's
WABA), so a local database that needs one generates one. Reading is
unchanged and stays tolerant: rows already stored with is_encrypted=false
keep decoding.

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


class CredentialEncryptionError(RuntimeError):
    """A secret was about to be written to the database in the clear.

    Raised, never folded into a return value, because the two outcomes are
    not comparable: one stores a token safely and the other publishes it to
    everyone who can read the table. Callers translate it into their own
    refusal — what they must not do is carry on."""


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


def encrypt_credential(value_dict: dict) -> tuple[str, bool]:
    """
    Encrypt a credential value dictionary for storage.

    Returns (ciphertext, True), or raises CredentialEncryptionError when
    there is no usable key.

    The raise is the whole point of this function, and there is deliberately
    no flag beside it. What arrives here is a provider's live secret — an
    OAuth token, a system-user token, an API key — and the previous
    behaviour was to write it as readable JSON whenever the key happened to
    be unset, which is a state a deployment reaches by doing nothing at all.
    Nothing downstream noticed: the row recorded is_encrypted=false, the API
    masked the value on read, and the plaintext sat in the column
    indefinitely.

    A flag whose only job is to restore that is a flag someone sets in
    production to unblock a deploy. Generating a key is one command, and the
    refusal below prints it.

    The bool is always True and stays in the return type because it is what
    the row's ``is_encrypted`` column records — rows written before this
    refusal existed carry False, and the read path still honours them.
    """
    json_str = json.dumps(value_dict)
    encrypted = encrypt_value(json_str)

    if encrypted is None:
        raise CredentialEncryptionError(
            "CREDENTIAL_ENCRYPTION_KEY is not set, or is not a valid "
            "base64-encoded 32-byte key, so this credential could only be "
            "stored as readable JSON. Refusing. Generate one with: "
            'python -c "import os,base64; '
            'print(base64.urlsafe_b64encode(os.urandom(32)).decode())"'
        )
    return encrypted, True


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
