"""Virtual try-on generation on Vertex AI.

Every call has one of four outcomes: an image, an empty reply, a safety
refusal, or a provider error. The first three are decided by the model and
the last two of those are not deterministic — the same photo and garment
pass one call and are refused the next, and setting every safety threshold
to OFF does not change that. So an empty reply and a refusal are both
retried, up to ``TRY_ON_MAX_ATTEMPTS`` in total; a provider error is not.
The caller bills on success only, so a failed attempt costs the merchant
no credits (it is still a provider call on our side). What the shopper is
told comes from the last outcome: a refusal is about the photo and the
item, an empty reply is a plain "try again".

The credentials are the ones the Vertex LLM providers already use. The
model and the instruction are dynamic config, so either can change without
a deploy.
"""

import asyncio
import base64
from typing import Optional, Tuple
from urllib.parse import urlparse

from google import genai
from google.genai import types

from app.ai.voice.llm._pools import get_genai_vertex_client
from app.core.config.dynamic import (
    GOOGLE_VERTEX_CREDENTIALS_JSON,
    GOOGLE_VERTEX_PROJECT_ID,
    TRY_ON_GENERATION_TIMEOUT_SECONDS,
    TRY_ON_INSTRUCTION,
    TRY_ON_MAX_ATTEMPTS,
    TRY_ON_MODEL,
)
from app.core.logger import logger
from app.core.transport.http_client import create_http_client

# The endpoint that serves the image models everywhere.
_LOCATION = "global"

# The garment is fetched by URL, so the host decides: a merchant CDN, never
# an address our own network can reach.
_ALLOWED_IMAGE_HOSTS = (".myshopify.com", ".shopify.com", ".shopifycdn.com")

# Position, not description, tells the model which image is which.
_PERSON_LABEL = "Image 1. This is the TARGET PERSON."
_GARMENT_LABEL = "Image 2. This is the GARMENT to dress the person in."

# Finish reasons that mean the provider refused to draw this photo in this
# garment, rather than failing to. Matched as substrings, so the IMAGE_*
# variants of each are covered by their stem.
_REFUSAL_REASONS = {"BLOCKLIST", "IMAGE_SAFETY", "PROHIBITED_CONTENT", "SAFETY", "SPII"}

# What one call to the model came back with, when not an image.
_EMPTY = "empty"
_REFUSED = "refused"

_MESSAGES = {
    _EMPTY: "The try-on could not be generated. Please try again.",
    _REFUSED: (
        "This item could not be shown on your photo. Try a clear, full-length "
        "photo of one person in fitted clothing, or try another item."
    ),
}
_CODES = {_EMPTY: "empty_result", _REFUSED: "photo_rejected"}

# The garment is a product photo on a CDN; anything larger is not one, and
# both the download and the request to the model carry it.
_MAX_GARMENT_BYTES = 16 * 1024 * 1024

# Wall-clock ceiling for that download. The HTTP client's own timeout is
# per chunk, so a host sending one byte at a time would never trip it.
_GARMENT_FETCH_SECONDS = 30

# The widget gives up on a try-on request after 300s (TRY_ON_TIMEOUT_MS in
# the client SDK). The whole request — the garment download and every
# attempt — has to fit inside that, or the shopper is told it took too long
# while the merchant is still paying for the attempts that follow.
_REQUEST_BUDGET_SECONDS = 300


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
    return get_genai_vertex_client(
        credentials_json=credentials_json,
        project_id=project_id,
        location=_LOCATION,
    )


async def _fetch_garment(url: str, where: str) -> types.Part:
    if not _is_allowed_image_url(url):
        logger.warning(f"try-on garment url refused {where}")
        raise TryOnGenerationError(
            "That product image cannot be used.", code="garment_rejected"
        )
    chunks: list[bytes] = []
    total = 0
    try:
        async with asyncio.timeout(_GARMENT_FETCH_SECONDS):
            async with create_http_client(timeout=_GARMENT_FETCH_SECONDS) as http:
                async with http.stream("GET", url) as response:
                    response.raise_for_status()
                    mime = response.headers.get("content-type", "image/jpeg").split(
                        ";"
                    )[0]
                    # Counted as it arrives: the cap must hold before the
                    # body is in memory, not after.
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_GARMENT_BYTES:
                            logger.warning(f"try-on garment too large {where}")
                            raise TryOnGenerationError(
                                "That product image cannot be used.",
                                code="garment_rejected",
                            )
                        chunks.append(chunk)
    except TryOnGenerationError:
        raise
    except Exception as exc:
        logger.error(f"try-on garment fetch failed {where}: {exc!r}")
        raise TryOnGenerationError(
            "Could not read the product image. Please try again.",
            code="garment_unreadable",
        ) from None
    return types.Part.from_bytes(
        data=b"".join(chunks),
        mime_type=mime if mime.startswith("image/") else "image/jpeg",
    )


def _instruction(template: str, product_title: str) -> str:
    """The instruction with the product named. A replace, not ``format``:
    the template is dynamic config, and a stray brace in it must not turn
    every try-on into an error."""
    title = product_title.replace('"', "'").strip()
    product = f'"{title}"' if title else "the garment this photo is selling"
    return template.replace("{product}", product)


async def try_on_attempts() -> int:
    """How many calls to the model this request may make: what config asks
    for, or what the budget leaves room for, whichever is smaller. Never
    fewer than one, whatever either says.

    The claim on the ``request_id`` is held for as long as this allows
    (see ``claim_try_on_request``), so both must read the same number: a
    claim that expired first would let a replay start a second generation
    on an id already being worked.
    """
    timeout_seconds = max(1, await TRY_ON_GENERATION_TIMEOUT_SECONDS())
    affordable = (_REQUEST_BUDGET_SECONDS - _GARMENT_FETCH_SECONDS) // timeout_seconds
    return max(1, min(await TRY_ON_MAX_ATTEMPTS(), affordable))


async def generate_try_on_image(
    *,
    merchant_domain: str,
    photo_bytes: bytes,
    photo_content_type: Optional[str],
    garment_image_url: str,
    product_id: Optional[str],
    request_id: str,
    product_title: str = "",
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
        types.Part.from_text(
            text=_instruction(await TRY_ON_INSTRUCTION(), product_title)
        ),
    ]

    model = await TRY_ON_MODEL()
    timeout_ms = int(await TRY_ON_GENERATION_TIMEOUT_SECONDS() * 1000)
    attempts = await try_on_attempts()
    outcome = _EMPTY
    for attempt in range(1, attempts + 1):
        image, outcome = await _attempt(client, parts, model, timeout_ms, where)
        if image:
            return image
        logger.warning(f"try-on {outcome} {where} attempt={attempt} of {attempts}")
    logger.error(f"try-on failed {where} outcome={outcome}")
    raise TryOnGenerationError(_MESSAGES[outcome], code=_CODES[outcome])


async def _attempt(
    client: genai.Client, parts: list, model: str, timeout_ms: int, where: str
) -> Tuple[Optional[str], str]:
    """One call to the model.

    Returns ``(image, outcome)``: the image as an ``<img src>`` with outcome
    ``"image"``, or ``None`` with the outcome that explains it. Raises
    :class:`TryOnGenerationError` only when the call itself failed.
    """
    try:
        response = await client.aio.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
                # Per request, so the pooled client is not keyed on it.
                http_options=types.HttpOptions(timeout=timeout_ms),
            ),
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
            return f"data:{mime};base64,{payload}", "image"

    finish_reason = str(getattr(candidate, "finish_reason", "") or "")
    blocked = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
    refused = bool(blocked) or any(r in finish_reason for r in _REFUSAL_REASONS)
    logger.warning(
        f"try-on returned no image {where} finish={finish_reason} {blocked=}"
    )
    return None, _REFUSED if refused else _EMPTY
