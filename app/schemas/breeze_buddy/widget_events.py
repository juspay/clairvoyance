"""Schemas for the widget client-event endpoint (POST /widget/events).

The assist widget runs inside merchant websites as an anonymous shopper.
It has no RBAC token, so it cannot use ``/client-logs`` — it authenticates
with the ``public_widget_key`` from its embed, the same credential
``POST /widget/session`` uses.

That anonymity drives the one design decision that matters here: the
event name is a **closed enum**, not free text. ``/client-logs`` accepts
arbitrary browser messages and therefore needs scrubbing, truncation and
control-character handling. This endpoint accepts nine known values, so
there is nothing to scrub — a caller cannot put arbitrary text into the
log stream at all. ``reason`` is the one free-ish field and is both
length-capped and charset-restricted.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_EVENTS_PER_BATCH = 10
MAX_REASON_CHARS = 64
MAX_VERSION_CHARS = 32
MAX_SESSION_ID_CHARS = 64
MAX_EVENT_MS = 600_000  # 10 minutes; anything larger is a broken clock


class WidgetEventName(str, Enum):
    """What the browser observed. Closed set: a client cannot invent one."""

    boot = "boot"  # widget mounted; carries version + load timing
    style_failed = "style_failed"  # custom-style-url never loaded
    skin_failed = "skin_failed"  # custom-skin-url handshake failed
    asset_failed = "asset_failed"  # product/logo image failed to load
    disconnected = "disconnected"
    reconnecting = "reconnecting"
    session_expired = "session_expired"
    rate_limited = "rate_limited"
    turn_error = "turn_error"


def _safe_token(value: Any, limit: int) -> Optional[str]:
    """Charset-restrict and clamp a short client string.

    These values become filter dimensions in the log aggregator, so they
    must stay boring. Restricting the charset (rather than scrubbing
    control characters, as ``/client-logs`` must) is possible here
    because every expected value is a machine-generated identifier.
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    cleaned = "".join(c for c in text.strip() if c.isalnum() or c in "._:-")
    return cleaned[:limit] or None


class WidgetEvent(BaseModel):
    """One observation. Counts, never one request per occurrence."""

    model_config = ConfigDict(extra="forbid")

    name: WidgetEventName
    count: int = Field(1, ge=1, le=999)
    reason: Optional[str] = Field(
        None, description="Short machine reason, e.g. a close code."
    )
    ms: Optional[int] = Field(None, ge=0, le=MAX_EVENT_MS)

    @field_validator("reason", mode="before")
    @classmethod
    def _clean_reason(cls, value: Any) -> Any:
        return _safe_token(value, MAX_REASON_CHARS)


class WidgetEventBatch(BaseModel):
    """Body of ``POST /agent/voice/breeze-buddy/widget/events``.

    ``session_id`` is optional on purpose: the widget mounts (and its
    stylesheet loads or 404s) on page load, long before a shopper opens
    the chat. Requiring a session would lose every visitor who never
    opened it — which is most of them, and they hit the same broken
    stylesheet.
    """

    model_config = ConfigDict(extra="forbid")

    public_widget_key: str = Field(..., min_length=10, max_length=255)
    session_id: Optional[str] = None
    widget_version: Optional[str] = None
    events: List[WidgetEvent] = Field(
        ..., min_length=1, max_length=MAX_EVENTS_PER_BATCH
    )

    @field_validator("session_id", mode="before")
    @classmethod
    def _clean_session_id(cls, value: Any) -> Any:
        return _safe_token(value, MAX_SESSION_ID_CHARS)

    @field_validator("widget_version", mode="before")
    @classmethod
    def _clean_version(cls, value: Any) -> Any:
        return _safe_token(value, MAX_VERSION_CHARS)


class WidgetEventIngestResponse(BaseModel):
    """Advisory receipt. The client may never read it (keepalive flush)."""

    accepted: int
