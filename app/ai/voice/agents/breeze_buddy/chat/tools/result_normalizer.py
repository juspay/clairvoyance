"""Unwrap the Clairvoyance + pipecat MCP envelope around tool results.

All vertical-specific shaping (numeric scaling, field flattening, image
precedence) used to live here. That logic now belongs in the template's
``tool_response_transforms`` (declarative) so this engine stays vertical-
agnostic. This module's only remaining job is to peel the two-layer
transport envelope so downstream callers see a plain dict (or whatever
the raw value was) instead of ``{"status": "...", "data": "<json>"}``.

The public ``normalize(tool_name, raw)`` signature is preserved for forward
compatibility — callers don't need to change, and a future per-tool hook can
slot in here without another agent-side refactor.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

_log = logging.getLogger(__name__)


def _unwrap_mcp_envelope(raw: Any) -> Optional[Any]:
    """Peel the Clairvoyance + pipecat MCP layering and return the inner value.

    Clairvoyance wraps MCP results as ``{"status": "success", "data": <value>}``;
    pipecat's ``MCPClient._call_tool`` concatenates ``text`` content chunks into
    a JSON-encoded string.  This helper unwraps both layers and returns the
    innermost value, or ``None`` if any layer is missing/unparseable.
    """
    if not isinstance(raw, dict):
        return None

    # 1. Clairvoyance envelope: {"status": "success"/"error", "data": ...}
    if "status" in raw and "data" in raw:
        if raw.get("status") != "success":
            return None
        raw = raw["data"]

    # 2. pipecat passes the MCP text-content as a raw JSON string — parse it.
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None

    return raw


def normalize(tool_name: str, raw: Any) -> Any:
    """Unwrap the Clairvoyance/pipecat MCP envelope around ``raw``.

    ``tool_name`` is kept for forward compatibility with potential per-tool
    hooks; the current body has no per-tool branching.  When the envelope is
    missing or unparseable, ``raw`` is returned unchanged so a malformed
    response never breaks a turn.
    """
    inner = _unwrap_mcp_envelope(raw)
    return inner if inner is not None else raw


__all__ = ["normalize"]
