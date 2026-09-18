"""Virtual try-on generation on Vertex AI.

A reply with no image at all is retried once: the same request usually
succeeds the second time, and the caller bills on success only, so the
shopper is never charged for the empty one. A refusal is never retried —
it is about this photo, and would be refused again.

The credentials are the ones the Vertex LLM providers already use, so
try-on adds no configuration of its own.
"""

import base64
from typing import Optional
from urllib.parse import urlparse

from google import genai
from google.genai import types

from app.ai.voice.llm._pools import get_genai_vertex_client
from app.core.config.dynamic import (
    GOOGLE_VERTEX_CREDENTIALS_JSON,
    GOOGLE_VERTEX_PROJECT_ID,
    TRY_ON_GENERATION_TIMEOUT_SECONDS,
)
from app.core.logger import logger
from app.core.transport.http_client import create_http_client

# Image-capable Gemini, on the endpoint that serves it everywhere.
_MODEL = "gemini-2.5-flash-image"
_LOCATION = "global"

# The garment is fetched by URL, so the host decides: a merchant CDN, never
# an address our own network can reach.
_ALLOWED_IMAGE_HOSTS = (".myshopify.com", ".shopify.com", ".shopifycdn.com")

# Position, not description, tells the model which image is which.
_PERSON_LABEL = "Image 1. This is the TARGET PERSON."
_GARMENT_LABEL = "Image 2. This is the GARMENT to dress the person in."
_INSTRUCTION = (
    "Generate a single photorealistic image of the person from Image 1 "
    "wearing the garment from Image 2. The person's facial features, skin "
    "tone, hair, and body proportions must match Image 1 exactly. The "
    "garment must fit naturally and look realistic."
)

# A refusal is about this photo: the same request would be refused again.
_REFUSAL_REASONS = {"IMAGE_SAFETY", "PROHIBITED_CONTENT", "SAFETY"}

# The garment is a product photo on a CDN; anything larger is not one, and
# both the download and the request to the model carry it.
_MAX_GARMENT_BYTES = 16 * 1024 * 1024


class TryOnGenerationError(Exception):
    """Generation produced no image. ``message`` is shopper-safe; ``code`` is
    for logs and metrics."""

    def __init__(self, message: str, code: str = "generation_failed") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _is_allowed_image_url(url: str) -> bool:
    """Whether we will fetch this garment URL (SSRF gate)."""
    try:
        parsed = urlparse(url if not url.startswith("//") else f"https:{url}")
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(
        host == suffix.lstrip(".") or host.endswith(suffix)
        for suffix in _ALLOWED_IMAGE_HOSTS
    )


async def _vertex_client() -> genai.Client:
    """The client the Vertex LLM providers are configured with."""
    credentials_json = await GOOGLE_VERTEX_CREDENTIALS_JSON()
    project_id = await GOOGLE_VERTEX_PROJECT_ID()
    if not credentials_json or not project_id:
        raise TryOnGenerationError(
            "Try-on is not configured for this deployment.", code="not_configured"
        )
    timeout = await TRY_ON_GENERATION_TIMEOUT_SECONDS()
    return get_genai_vertex_client(
        credentials_json=credentials_json,
        project_id=project_id,
        location=_LOCATION,
        timeout_ms=int(timeout * 1000),
    )


async def _fetch_garment(url: str, where: str) -> types.Part:
    if not _is_allowed_image_url(url):
        logger.warning(f"try-on garment url refused {where}")
        raise TryOnGenerationError(
            "That product image cannot be used.", code="garment_rejected"
        )
    try:
        async with create_http_client(timeout=30) as http:
            response = await http.get(url)
            response.raise_for_status()
    except Exception as exc:
        logger.error(f"try-on garment fetch failed {where}: {exc}")
        raise TryOnGenerationError(
            "Could not read the product image. Please try again.",
            code="garment_unreadable",
        ) from None
    if len(response.content) > _MAX_GARMENT_BYTES:
        logger.warning(
            f"try-on garment too large {where} bytes={len(response.content)}"
        )
        raise TryOnGenerationError(
            "That product image cannot be used.", code="garment_rejected"
        )
    mime = response.headers.get("content-type", "image/jpeg").split(";")[0]
    return types.Part.from_bytes(
        data=response.content,
        mime_type=mime if mime.startswith("image/") else "image/jpeg",
    )


async def generate_try_on_image(
    *,
    merchant_domain: str,
    photo_bytes: bytes,
    photo_content_type: Optional[str],
    garment_image_url: str,
    product_id: Optional[str],
    request_id: str,
) -> str:
    """Generate one try-on image and return it as an ``<img src>``.

    Raises:
        TryOnGenerationError: on any outcome that is not an image. The
        caller treats them all alike — tell the shopper, charge nothing.
    """
    where = f"merchant={merchant_domain} product={product_id} request_id={request_id}"
    client = await _vertex_client()
    person_mime = (
        photo_content_type
        if photo_content_type and photo_content_type.startswith("image/")
        else "image/jpeg"
    )
    parts = [
        types.Part.from_text(text=_PERSON_LABEL),
        types.Part.from_bytes(data=photo_bytes, mime_type=person_mime),
        types.Part.from_text(text=_GARMENT_LABEL),
        await _fetch_garment(garment_image_url, where),
        types.Part.from_text(text=_INSTRUCTION),
    ]

    for attempt in (1, 2):
        image = await _attempt(client, parts, where)
        if image:
            return image
        logger.warning(f"try-on empty reply {where} attempt={attempt}")
    raise TryOnGenerationError(
        "The try-on could not be generated. Please try again.", code="empty_result"
    )


async def _attempt(client: genai.Client, parts: list, where: str) -> Optional[str]:
    """One generation. Returns the image, or None when the reply carried
    none. Raises when the provider refused this photo."""
    try:
        response = await client.aio.models.generate_content(
            model=_MODEL,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"]),
        )
    except Exception as exc:
        logger.error(f"try-on generation call failed {where}: {exc}")
        raise TryOnGenerationError(
            "The try-on could not be generated. Please try again.",
            code="provider_error",
        ) from None

    candidate = (response.candidates or [None])[0]
    content = getattr(candidate, "content", None)
    for part in getattr(content, "parts", None) or []:
        inline = getattr(part, "inline_data", None)
        if inline and inline.data:
            mime = inline.mime_type or "image/png"
            payload = base64.b64encode(inline.data).decode("ascii")
            return f"data:{mime};base64,{payload}"

    # No image: the reason decides whether a retry could ever help.
    finish_reason = str(getattr(candidate, "finish_reason", "") or "")
    blocked = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
    logger.error(f"try-on returned no image {where} finish={finish_reason} {blocked=}")
    if blocked or any(reason in finish_reason for reason in _REFUSAL_REASONS):
        raise TryOnGenerationError(
            "That photo could not be used. Try a clear, well-lit photo "
            "of one person, facing the camera.",
            code="photo_rejected",
        )
    return None
