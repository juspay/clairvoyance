"""Provider-neutral website scraping service."""

from dataclasses import dataclass
from typing import Any, Dict, List

from app.services.scraper.website.gemini.scraper import scrape_website_with_gemini


@dataclass
class WebsiteScrapingResult:
    text: str
    provider: str
    status: str
    provider_response: Dict[str, str]
    url_context_metadata: List[Dict[str, str]]


async def scrape_website(
    *,
    provider: str,
    provider_config: Dict[str, Any],
    url: str,
    timeout_seconds: int = 18,
) -> WebsiteScrapingResult:
    """Scrape website context using the requested backend provider."""
    if not provider:
        raise ValueError("provider is required")
    if provider == "google":
        result = await scrape_website_with_gemini(
            provider_config=provider_config,
            url=url,
            timeout_seconds=timeout_seconds,
        )
        return WebsiteScrapingResult(
            text=result.generated_text,
            provider=provider,
            status=result.generation_status,
            provider_response={
                "model": result.model,
                "input_prompt_hash": result.input_prompt_hash,
                "output_hash": result.output_hash,
            },
            url_context_metadata=result.url_context_metadata,
        )

    raise ValueError(f"Unsupported website scraping provider: {provider}")
