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
    """
    if not sign_key:
        return ""

    # Generate HMAC-SHA256 signature
    signature = hmac.new(
        sign_key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).digest()

    # Encode to base64
    return base64.b64encode(signature).decode("utf-8")
