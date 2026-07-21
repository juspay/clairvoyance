"""Provider-neutral website scraping service."""

from dataclasses import dataclass
from typing import Dict, List, Optional

from app.services.gemini_prompt_generation import generate_gemini_prompt


@dataclass
class WebsiteScrapingResult:
    generated_text: str
    service_name: str
    model: str
    generation_status: str
    url_context_metadata: List[Dict[str, str]]
    input_prompt_hash: str
    output_hash: str


async def scrape_website(
    *,
    service_name: str,
    prompt: Optional[str] = None,
    url: Optional[str] = None,
    use_url_context: bool = True,
    temperature: float = 0.1,
    max_output_tokens: int = 8192,
    timeout_seconds: int = 18,
) -> WebsiteScrapingResult:
    """Scrape website context using the requested backend service."""
    if not service_name:
        raise ValueError("serviceName is required")
    if service_name == "gemini":
        result = await generate_gemini_prompt(
            prompt=prompt,
            url=url,
            use_url_context=use_url_context,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
        return WebsiteScrapingResult(
            generated_text=result.generated_text,
            service_name=service_name,
            model=result.model,
            generation_status=result.generation_status,
            url_context_metadata=result.url_context_metadata,
            input_prompt_hash=result.input_prompt_hash,
            output_hash=result.output_hash,
        )

    raise ValueError(f"Unsupported website scraping service: {service_name}")
