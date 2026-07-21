"""Schemas for provider-neutral website scraping."""

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class WebsiteScrapingRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    service_name: str = Field(
        default="gemini",
        alias="serviceName",
        description="Website scraping service to use. Currently implemented: gemini.",
    )
    prompt: Optional[str] = None
    url: Optional[str] = None
    use_url_context: bool = Field(default=True, alias="useUrlContext")
    temperature: float = 0.1
    max_output_tokens: int = Field(default=8192, alias="maxOutputTokens")
    timeout_seconds: int = Field(default=18, alias="timeoutSeconds")


class WebsiteScrapingResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    success: bool = True
    service_name: str = Field(alias="serviceName")
    generated_text: str = Field(alias="generatedText")
    generation_status: str = Field(alias="generationStatus")
    model: str
    url_context_metadata: List[Dict[str, str]] = Field(alias="urlContextMetadata")
    input_prompt_hash: str = Field(alias="inputPromptHash")
    output_hash: str = Field(alias="outputHash")
    error: Optional[str] = None
