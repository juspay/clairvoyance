"""Nautilus client for virtual try-on generation.

One call, one attempt. A retry here would be a second provider charge for
a shopper already waiting, and the caller bills on success only. The
widget offers the retry, as a fresh request with a fresh ``request_id``.
"""

import base64
from typing import Any, Dict, Optional

import httpx

from app.core.config.dynamic import TRY_ON_GENERATION_TIMEOUT_SECONDS
from app.core.config.static import NAUTILUS_TRY_ON_SECRET, NAUTILUS_TRY_ON_URL
from app.core.logger import logger
from app.core.transport.http_client import create_http_client


class TryOnGenerationError(Exception):
    """Generation produced no image. ``message`` is shopper-safe; ``code`` is
    for logs and metrics."""

    def __init__(self, message: str, code: str = "generation_failed") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _data_uri(photo_bytes: bytes, content_type: Optional[str]) -> str:
    """Wrap the upload as a data URI. Nautilus validates the declared MIME."""
    mime = (
        content_type
        if content_type and content_type.startswith("image/")
        else "image/jpeg"
    )
    return f"data:{mime};base64,{base64.b64encode(photo_bytes).decode('ascii')}"


def _first_image(payload: Dict[str, Any]) -> Optional[str]:
    """Pull an ``<img src>`` out of nautilus's results array.

    Providers return a hosted URL or raw base64, with or without the
    data-URI prefix.
    """
    results = payload.get("results")
    first = results[0] if isinstance(results, list) and results else None
    if not isinstance(first, dict):
        return None

    image_url = first.get("imageUrl")
    if isinstance(image_url, str) and image_url:
        return image_url

    image_b64 = first.get("imageBase64")
    if not isinstance(image_b64, str) or not image_b64:
        return None
    if image_b64.startswith("data:"):
        return image_b64

    metadata = first.get("metadata")
    mime = metadata.get("mimeType") if isinstance(metadata, dict) else None
    return f"data:{mime if isinstance(mime, str) else 'image/png'};base64,{image_b64}"


async def generate_try_on_image(
    *,
    merchant_domain: str,
    photo_bytes: bytes,
    photo_content_type: Optional[str],
    garment_image_url: str,
    mode: str,
    product_id: Optional[str],
    request_id: str,
) -> str:
    """Generate one try-on image and return it as an ``<img src>``.

    Raises:
        TryOnGenerationError: on any outcome that is not an image. The
        caller treats them all alike — tell the shopper, charge nothing.
    """
    if not NAUTILUS_TRY_ON_SECRET:
        raise TryOnGenerationError(
            "Try-on is not configured for this deployment.", code="not_configured"
        )

    body = {
        "shopDomain": merchant_domain,
        "personImage": _data_uri(photo_bytes, photo_content_type),
        "garmentImage": garment_image_url,
        "mode": mode,
        "productId": product_id,
        "requestId": request_id,
    }
    timeout = await TRY_ON_GENERATION_TIMEOUT_SECONDS()
    where = f"merchant={merchant_domain} request_id={request_id}"

    try:
        async with create_http_client(timeout=timeout) as client:
            response = await client.post(
                NAUTILUS_TRY_ON_URL,
                json=body,
                headers={"Authorization": f"Bearer {NAUTILUS_TRY_ON_SECRET}"},
            )
    except httpx.TimeoutException:
        logger.warning(f"try-on timed out {where} after={timeout}s")
        raise TryOnGenerationError(
            "The try-on took too long. Please try again.", code="timeout"
        ) from None
    except httpx.HTTPError as exc:
        logger.error(f"try-on transport error {where}: {exc}")
        raise TryOnGenerationError(
            "Could not reach the try-on service. Please try again.",
            code="transport_error",
        ) from None

    if response.status_code != 200:
        # Nautilus's own error text: safe to log, never shown raw.
        logger.error(
            f"try-on rejected {where} status={response.status_code} "
            f"body={response.text[:300]}"
        )
        # 422 is a refusal of THIS photo, so "try again" would send the
        # shopper back into the same answer.
        if response.status_code == 422:
            raise TryOnGenerationError(
                "That photo could not be used. Try a clear, well-lit photo "
                "of one person, facing the camera.",
                code="photo_rejected",
            )
        raise TryOnGenerationError(
            "The try-on could not be generated. Please try again.",
            code=f"http_{response.status_code}",
        )

    try:
        payload = response.json()
    except ValueError:
        raise TryOnGenerationError(
            "The try-on service returned an unreadable response.", code="bad_response"
        ) from None

    image = _first_image(payload if isinstance(payload, dict) else {})
    if not image:
        logger.error(f"try-on generation returned no image {where}")
        raise TryOnGenerationError(
            "The try-on service returned no image.", code="empty_result"
        )
    return image
