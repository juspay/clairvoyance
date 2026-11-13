import base64
import hashlib
import hmac


def calculate_hmac_sha256(value: str, sign_key: str) -> str:
    """
    Generate HMAC-SHA256 signature and encode to base64.
    This matches the Haskell function: calculateHmacSha256 :: Text -> Text -> Text

    Args:
        value: The message to sign
        sign_key: The secret key for signing

    Returns:
        Base64-encoded HMAC-SHA256 signature

    Raises:
        ValueError: ``sign_key`` is empty. There is no signature without a
        key, and returning "" let a caller send or compare an unsigned
        message as if it were signed.
    """
    if not sign_key:
        raise ValueError("calculate_hmac_sha256 needs a non-empty sign_key")

    # Generate HMAC-SHA256 signature
    signature = hmac.new(
        sign_key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).digest()

    # Encode to base64
    return base64.b64encode(signature).decode("utf-8")
