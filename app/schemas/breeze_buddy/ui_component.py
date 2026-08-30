"""Pydantic models for ui_component rows (migration 057).

The custom-component registry: merchant-specific UI components stored as
DATA — a JSON-Schema props contract, engine-read flags, and an optional
declarative ``render_def`` the widget interprets. See
``ai/voice/agents/breeze_buddy/chat/ui/custom_defs.py`` for the write-time
guards and the hydration path.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class UiComponentCreate(BaseModel):
    """Body of ``POST /ui-components``."""

    reseller_id: str = Field(..., min_length=1, max_length=255)
    merchant_id: Optional[str] = Field(
        None,
        max_length=255,
        description="NULL/omitted = reseller-wide component.",
    )
    name: str = Field(..., min_length=1, max_length=128)
    props_schema: Dict[str, Any] = Field(
        ..., description="JSON Schema (draft 2020-12) for the hydrated props."
    )
    flags: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Engine flags: data_bound (must be true), selection_field, "
            "list_props, max_items_default, max_items_limit."
        ),
    )
    render_def: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Declarative render tree for our widget/skins. Omit for "
            "merchants rendering with their own frontend."
        ),
    )
    prompt_hint: Optional[str] = Field(None, max_length=2000)
    is_active: bool = True


class UiComponentUpdate(BaseModel):
    """Body of ``PUT /ui-components/{id}``. Any supplied field updates;
    every update bumps ``version``."""

    props_schema: Optional[Dict[str, Any]] = None
    flags: Optional[Dict[str, Any]] = None
    render_def: Optional[Dict[str, Any]] = None
    prompt_hint: Optional[str] = Field(None, max_length=2000)
    is_active: Optional[bool] = None


class UiComponentResponse(BaseModel):
    """One ui_component row."""

    id: str
    reseller_id: str
    merchant_id: Optional[str] = None
    name: str
    version: int = 1
    props_schema: Dict[str, Any]
    flags: Dict[str, Any] = Field(default_factory=dict)
    render_def: Optional[Dict[str, Any]] = None
    prompt_hint: Optional[str] = None
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class UiComponentListResponse(BaseModel):
    """Body of ``GET /ui-components``."""

    ui_components: List[UiComponentResponse]
    total: int
    page: int = 1
