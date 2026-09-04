"""Schemas for the browser log-shipping endpoint (POST /client-logs).

The loom frontend redacts secrets and caps every field before sending;
see loom ``src/lib/logging/redact.ts``. This file does not repeat that
work. What it enforces is only what the frontend cannot be trusted for,
because any holder of an RBAC token can POST here directly with curl:

  1. ``extra="forbid"`` — the caller cannot smuggle a field the backend
     stamps itself (user id, source, ...). See client_logs/handlers.py.
  2. The level enum has no CRITICAL member, so a client can never reach
     a loguru level above ERROR and page an on-call.

Size is bounded by the 64 KiB body cap in ``client_logs/__init__.py``,
which bounds every field in a batch transitively. There are no per-field
caps here on purpose: they only duplicated the frontend's own clamping,
and any mismatch between the two limits would 422 a batch the frontend
had already trimmed to fit.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

MAX_ENTRIES_PER_BATCH = 50
# fetch(..., {keepalive: true}) — the pagehide flush — hard-caps the
# request body at 64 KiB. Matching it here means anything this endpoint
# accepts is guaranteed sendable while the page is closing.
MAX_BODY_BYTES = 64 * 1024


class ClientLogLevel(str, Enum):
    """Levels a browser may emit.

    No CRITICAL member, by design: this enum is the hard ceiling that
    stops a client paging an on-call. See ``handlers._LEVEL_MAP``.
    """

    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class ClientLogEntry(BaseModel):
    """One browser log line. All fields client-supplied and untrusted."""

    model_config = ConfigDict(extra="forbid")

    level: ClientLogLevel = ClientLogLevel.INFO
    message: str
    channel: Optional[str] = None  # "api" | "window.error" | "manual" | ...
    stack: Optional[str] = None
    url: Optional[str] = None  # page path where it happened (no query)
    # Kept as a plain string. The value is only ever logged, never compared
    # or stored, so parsing it buys nothing — and a client with a broken
    # clock would 422 an otherwise good batch.
    client_ts: Optional[str] = None
    context: Optional[Dict[str, Any]] = None


class ClientLogBatch(BaseModel):
    """Body of ``POST /agent/voice/breeze-buddy/client-logs``."""

    model_config = ConfigDict(extra="forbid")

    entries: List[ClientLogEntry] = Field(
        ..., min_length=1, max_length=MAX_ENTRIES_PER_BATCH
    )
    session_id: Optional[str] = None  # client-generated per-tab id


class ClientLogIngestResponse(BaseModel):
    """Advisory receipt. The client may never read it (keepalive flush)."""

    accepted: int
    dropped: int = 0
