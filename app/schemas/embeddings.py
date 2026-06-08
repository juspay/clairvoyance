"""Shared embedding-provider configuration."""

from typing import Literal

from pydantic import BaseModel, Field


class EmbeddingConfig(BaseModel):
    """Provider/model snapshot used by every vector-backed feature."""

    provider: Literal["azure_openai", "openai"] = "azure_openai"
    model: str = Field("text-embedding-3-large", min_length=1)
    dimensions: Literal[768] = 768
