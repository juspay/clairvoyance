"""Response model for ``DELETE /admin/templates/{id}/purge``."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class TemplatePurgeBlockers(BaseModel):
    """Live references that make a purge refuse (all must be zero)."""

    widget_configs: int = 0
    call_configs: int = 0
    inflight_leads: int = 0


class TemplatePurgeTemplate(BaseModel):
    id: str
    name: str
    reseller_id: Optional[str] = None
    merchant_id: Optional[str] = None
    is_active: bool = True


class TemplatePurgeResponse(BaseModel):
    """What a purge removed, or with ``dry_run`` what it would remove."""

    purged: bool = Field(description="False on a dry run; nothing was deleted.")
    template: TemplatePurgeTemplate
    chat_sessions: int = Field(
        description="Chat sessions removed (dry run: that would be removed)."
    )
    chat_messages: int = Field(
        description="Messages under those sessions (cascade from the session)."
    )
    active_sessions: int = Field(
        default=0,
        description="Sessions still marked ACTIVE among those; stale on an unbound template.",
    )
    blockers: TemplatePurgeBlockers = Field(default_factory=TemplatePurgeBlockers)


__all__ = ["TemplatePurgeBlockers", "TemplatePurgeResponse", "TemplatePurgeTemplate"]
