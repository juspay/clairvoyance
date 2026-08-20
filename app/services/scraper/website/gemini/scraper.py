"""Gemini-backed website scraping service."""

import asyncio
import hashlib
import ipaddress
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from google import genai
from google.genai.types import GenerateContentConfig
from pydantic import BaseModel

from app.core.config.dynamic import GEMINI_SCRAPER_MODEL
from app.core.config.static import GEMINI_API_KEY
from app.core.logger import logger
from app.services.scraper.website.exceptions import (
    WebsiteScrapingConfigurationError,
    WebsiteScrapingUpstreamError,
)

DEFAULT_GEMINI_SCRAPER_PROMPT = (
    "You have access to internet search, URL context, and related online sources. "
    "Visit and analyze the provided website URL. Extract concise factual website "
    "context for building an assistant. Include what the business sells or does, "
    "key categories, important products or services, offers, shipping or delivery "
    "information, returns or refunds, payment information, support or contact "
    "details, trust claims, brand tone, and target audience when available. Use "
    "only information found on the website or related online sources. Do not "
    "invent missing details."
)


@dataclass
class GeminiWebsiteScrapingResult:
    generated_text: str
    model: str
    generation_status: str
    url_context_metadata: List[Dict[str, str]]
    input_prompt_hash: str
    output_hash: str


class GeminiWebsiteScrapingConfig(BaseModel):
    prompt: Optional[str] = None
    use_url_context: bool = True
    use_google_search: bool = True
    temperature: float = 0.1
    max_output_tokens: int = 8192


async def scrape_website_with_gemini(
    *,
    provider_config: Dict[str, Any],
    url: Optional[str] = None,
    timeout_seconds: int = 18,
) -> GeminiWebsiteScrapingResult:
    """Scrape website context using Gemini tools."""
    config_model = GeminiWebsiteScrapingConfig.model_validate(provider_config)
    normalized_prompt = (
        config_model.prompt or ""
    ).strip() or DEFAULT_GEMINI_SCRAPER_PROMPT
    normalized_url = _normalize_url(url)
    if not normalized_url:
        raise ValueError("url is required")
    if not GEMINI_API_KEY:
        raise WebsiteScrapingConfigurationError("GEMINI_API_KEY is not configured")

    model = await GEMINI_SCRAPER_MODEL()
    generation_params: Dict[str, Any] = {
        "temperature": config_model.temperature,
        "max_output_tokens": config_model.max_output_tokens,
    }
    tools: List[Dict[str, Dict[str, Any]]] = []
    if config_model.use_url_context:
        tools.append({"url_context": {}})
    if config_model.use_google_search:
        tools.append({"google_search": {}})
    if tools:
        generation_params["tools"] = tools
    config = GenerateContentConfig(**generation_params)

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=model,
            contents=_build_contents(normalized_prompt, normalized_url),
            config=config,
        ),
        timeout=timeout_seconds,
    )

    generated_text = (getattr(response, "text", "") or "").strip()
    if not generated_text:
        raise WebsiteScrapingUpstreamError("Gemini returned an empty response")

    url_context_metadata = _extract_url_context_metadata(response)
    logger.info(
        "Gemini prompt generation completed",
        model=model,
        prompt_length=len(normalized_prompt),
        url=normalized_url,
        response_length=len(generated_text),
        use_url_context=config_model.use_url_context,
        use_google_search=config_model.use_google_search,
        retrieved_urls=url_context_metadata,
    )

    return GeminiWebsiteScrapingResult(
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
    if len(raw_url) > 2048:
        raise ValueError("url is too long")

    if raw_url.startswith("http://"):
        raise ValueError("url must be a valid https URL")

    normalized_url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
    parsed = urlparse(normalized_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("url must be a valid https URL")

    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith((".local", ".internal")):
        raise ValueError("url must use a public host")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("url must use a public host")

    if parsed.port and parsed.port != 443:
        raise ValueError("url must use the default https port")
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
