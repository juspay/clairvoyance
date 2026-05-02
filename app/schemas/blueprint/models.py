"""
Pydantic schemas for Blueprint agent model selection.
"""

from pydantic import BaseModel


class ModelOption(BaseModel):
    id: str
    name: str
    description: str
    recommended_for: list[str]


class ModelsListResponse(BaseModel):
    models: list[ModelOption]
