"""Generic Gemini prompt generation service."""

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from google import genai
from google.genai.types import GenerateContentConfig

from app.core.config.dynamic import GEMINI_PROMPT_GENERATION_MODEL
from app.core.config.static import GEMINI_API_KEY
from app.core.logger import logger


@dataclass
class GeminiPromptGenerationResult:
    generated_text: str
    model: str
    generation_status: str
    url_context_metadata: List[Dict[str, str]]
    input_prompt_hash: str
    output_hash: str


async def generate_gemini_prompt(
    *,
    prompt: Optional[str] = None,
    url: Optional[str] = None,
    use_url_context: bool = True,
    temperature: float = 0.1,
    max_output_tokens: int = 8192,
    timeout_seconds: int = 18,
) -> GeminiPromptGenerationResult:
    """Generate text using Gemini from a caller-provided prompt."""
    normalized_prompt = (prompt or "").strip()
    normalized_url = _normalize_url(url)
    if not normalized_prompt and not normalized_url:
        raise ValueError("prompt or url is required")
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured")

    model = await GEMINI_PROMPT_GENERATION_MODEL()
    generation_params: Dict[str, Any] = {
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
    }
    if use_url_context:
        generation_params["tools"] = [{"url_context": {}}]
    config = GenerateContentConfig(**generation_params)

    def call_gemini():
        client = genai.Client(api_key=GEMINI_API_KEY)
        return client.models.generate_content(
            model=model,
            contents=_build_contents(normalized_prompt, normalized_url),
            config=config,
        )

    response = await asyncio.wait_for(
        asyncio.to_thread(call_gemini), timeout=timeout_seconds
    )

    generated_text = (getattr(response, "text", "") or "").strip()
    if not generated_text:
        raise ValueError("Gemini returned an empty response")

    url_context_metadata = _extract_url_context_metadata(response)
    logger.info(
        "Gemini prompt generation completed",
        model=model,
        prompt_length=len(normalized_prompt),
        url=normalized_url,
        response_length=len(generated_text),
        use_url_context=use_url_context,
        retrieved_urls=url_context_metadata,
    )

    return GeminiPromptGenerationResult(
        generated_text=generated_text,
        model=model,
        generation_status="generated",
        url_context_metadata=url_context_metadata,
        input_prompt_hash=_hash_text(
            _build_contents(normalized_prompt, normalized_url)
        ),
        output_hash=_hash_text(generated_text),
    )


def _normalize_url(url: Optional[str]) -> Optional[str]:
    raw_url = (url or "").strip()
    if not raw_url:
        return None

    normalized_url = (
        raw_url if raw_url.startswith(("http://", "https://")) else f"https://{raw_url}"
    )
    parsed = urlparse(normalized_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ValueError("url must be a valid http(s) URL")
    if parsed.port and parsed.port not in {80, 443}:
        raise ValueError("url must use the default http(s) port")
    return normalized_url


def _build_contents(prompt: str, url: Optional[str]) -> str:
    if prompt and not url:
        return prompt
    if url and not prompt:
        return f"URL: {url}"
    return f"{prompt}\n\nURL: {url}"


def _extract_url_context_metadata(response: Any) -> List[Dict[str, str]]:
    metadata = getattr(response, "url_context_metadata", None) or getattr(
        response, "urlContextMetadata", None
    )
    url_metadata = getattr(metadata, "url_metadata", None) or getattr(
        metadata, "urlMetadata", None
    )
    if not url_metadata:
        return []

    retrieved = []
    for entry in url_metadata:
        url = getattr(entry, "retrieved_url", None) or getattr(
            entry, "retrievedUrl", None
        )
        if not url:
            continue
        status = getattr(entry, "url_retrieval_status", None) or getattr(
            entry, "urlRetrievalStatus", None
        )
        retrieved.append(
            {
                "url": str(url),
                "status": str(status or "URL_RETRIEVAL_STATUS_UNSPECIFIED"),
            }
        )
    return retrieved


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
