"""Decrypt Nautilus-to-Clairvoyance WhatsApp token envelopes."""

import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config.static import WHATSAPP_SYNC_PRIVATE_KEY
from app.schemas.breeze_buddy.whatsapp import EncryptedWhatsAppAccessToken


def _decode_base64_field(field_name: str, value: str) -> bytes:
    """Decode a base64 field without including the value in errors."""
    try:
        return base64.b64decode(value, validate=True)
    except Exception as e:
        raise ValueError(f"Invalid base64 for {field_name}") from e


def _load_private_key() -> RSAPrivateKey:
    """Load the RSA private key configured for Nautilus sync payloads."""
    raw_key = WHATSAPP_SYNC_PRIVATE_KEY.strip()
    if not raw_key:
        raise ValueError("WHATSAPP_SYNC_PRIVATE_KEY is not configured")

    pem = raw_key.replace("\\n", "\n")
    if "BEGIN" not in pem:
        pem = _decode_base64_field("WHATSAPP_SYNC_PRIVATE_KEY", raw_key).decode("utf-8")

    private_key = serialization.load_pem_private_key(
        pem.encode("utf-8"),
        password=None,
    )
    if not isinstance(private_key, RSAPrivateKey):
        raise ValueError("WHATSAPP_SYNC_PRIVATE_KEY must be an RSA private key")

    return private_key


def decrypt_whatsapp_access_token_envelope(
    envelope: EncryptedWhatsAppAccessToken,
) -> str:
    """Decrypt a Nautilus encrypted Meta access token envelope."""
    encrypted_key = _decode_base64_field("encrypted_key", envelope.encrypted_key)
    iv = _decode_base64_field("iv", envelope.iv)
    auth_tag = _decode_base64_field("auth_tag", envelope.auth_tag)
    ciphertext = _decode_base64_field("ciphertext", envelope.ciphertext)

    private_key = _load_private_key()
    aes_key = private_key.decrypt(
        encrypted_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    if len(aes_key) != 32:
        raise ValueError("Decrypted WhatsApp token key must be 32 bytes")

    try:
        token = AESGCM(aes_key).decrypt(iv, ciphertext + auth_tag, None)
        return token.decode("utf-8")
    except Exception as e:
        raise ValueError("Failed to decrypt WhatsApp access token envelope") from e
