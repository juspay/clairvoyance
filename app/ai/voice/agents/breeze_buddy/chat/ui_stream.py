"""SpecStream / A2UI wire format — JSONL emission pipeline.

Replaces the OpenUI Lang DSL (``<ui>…</ui>`` + ``@Each`` / ``Card([…])``
grammar) with a flat JSON-patch stream.

The LLM emits operations one-per-line, wrapped in a literal
``<ui_stream>…</ui_stream>`` sentinel::

    <ui_stream>
    {"op":"add","id":"root","type":"Carousel"}
    {"op":"add","id":"t1","type":"Tile","parent":"root","props":{"title":"Dawn","body":[{"kind":"key_value","key":"Price","value":"₹699.95"}],"actions":[{"label":"View","action":{"type":"to_assistant","msg":"Tell me about Dawn"}}]}}
    {"op":"remove","id":"c2"}
    </ui_stream>

Each complete line is parsed → optionally healed (S1.2) → catalog-validated
→ surfaced as an ``ui_op`` SSE event. The widget applies the op to a
session-stateful ``ui_state`` store; the renderer walks the tree.

The marker may straddle token boundaries (Anthropic in particular tokenises
one char at a time) — we keep a bounded carry buffer.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Literal, Optional, Set, Union

from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    is_known_type,
    validate_props,
)

_log = logging.getLogger(__name__)

_OPEN = "<ui_stream>"
_CLOSE = "</ui_stream>"
_CARRY_MAX = max(len(_OPEN), len(_CLOSE)) - 1


# ---------------------------------------------------------------------------
# Streaming items
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextOut:
    """Prose chunk to forward as an ``assistant_token`` SSE event."""

    value: str
    kind: Literal["text"] = "text"


@dataclass(frozen=True)
class JsonlOpLine:
    """One JSONL line extracted from inside a ``<ui_stream>`` block.

    The raw string is preserved verbatim so the healer (S1.2) can fix
    malformed JSON before the parser/validator runs.
    """

    raw: str
    kind: Literal["op_line"] = "op_line"


YieldItem = Union[TextOut, JsonlOpLine]


# ---------------------------------------------------------------------------
# Streaming extractor
# ---------------------------------------------------------------------------


class UiStreamExtractor:
    """Stateful FSM that pulls ``<ui_stream>…</ui_stream>`` blocks out of an
    assistant text stream, yielding TextOut for prose and JsonlOpLine for
    each complete line of JSONL seen inside a block.

    Public API: ``feed(delta)`` per text delta, ``flush()`` at stream end.
    """

    def __init__(self) -> None:
        # OUTSIDE — collecting prose, looking for "<ui_stream>".
        # INSIDE — collecting JSONL, splitting on newlines, looking for "</ui_stream>".
        self._state: Literal["OUTSIDE", "INSIDE"] = "OUTSIDE"
        # Bytes held back because they might be the start of a marker.
        self._carry: str = ""
        # Partial JSONL line accumulator (active while INSIDE).
        self._line_buf: str = ""

    def feed(self, delta: str) -> Iterator[YieldItem]:
        """Process one text delta. Yields zero or more text/op_line items."""
        if not delta:
            return
        buffer = self._carry + delta
        self._carry = ""
        yield from self._process(buffer)

    def flush(self) -> Iterator[YieldItem]:
        """Drain held state at stream end.

        Outside a block: forward residual carry as prose.
        Inside a block: drop partial JSONL with a warning — the LLM opened
        ``<ui_stream>`` but never closed it.
        """
        if self._state == "OUTSIDE":
            if self._carry:
                yield TextOut(value=self._carry)
            self._carry = ""
            return
        partial = self._line_buf + self._carry
        _log.warning(
            "ui_stream: stream ended inside <ui_stream> block (dropping %d chars)",
            len(partial),
        )
        self._line_buf = ""
        self._carry = ""
        self._state = "OUTSIDE"

    # ---- Internal FSM ----------------------------------------------------

    def _process(self, buffer: str) -> Iterator[YieldItem]:
        while buffer:
            if self._state == "OUTSIDE":
                idx = buffer.find(_OPEN)
                if idx != -1:
                    if idx > 0:
                        yield TextOut(value=buffer[:idx])
                    buffer = buffer[idx + len(_OPEN) :]
                    self._state = "INSIDE"
                    continue
                hold = _tail_marker_prefix(buffer, _OPEN)
                if hold:
                    if len(buffer) > hold:
                        yield TextOut(value=buffer[:-hold])
                    self._carry = buffer[-hold:]
                else:
                    yield TextOut(value=buffer)
                    self._carry = ""
                return

            # INSIDE — first check if the close marker is anywhere ahead.
            close_idx = buffer.find(_CLOSE)
            newline_idx = buffer.find("\n")

            # No close, no newline — accumulate partial line (carrying any
            # trailing chars that could start the close marker).
            if close_idx == -1 and newline_idx == -1:
                hold = _tail_marker_prefix(buffer, _CLOSE)
                if hold:
                    self._line_buf += buffer[:-hold]
                    self._carry = buffer[-hold:]
                else:
                    self._line_buf += buffer
                    self._carry = ""
                return

            # Close before newline? Flush remaining line then exit block.
            if close_idx != -1 and (newline_idx == -1 or close_idx < newline_idx):
                self._line_buf += buffer[:close_idx]
                line = self._line_buf.strip()
                self._line_buf = ""
                if line:
                    yield JsonlOpLine(raw=line)
                buffer = buffer[close_idx + len(_CLOSE) :]
                self._state = "OUTSIDE"
                continue

            # Newline first — yield the complete line, continue inside.
            self._line_buf += buffer[:newline_idx]
            line = self._line_buf.strip()
            self._line_buf = ""
            if line:
                yield JsonlOpLine(raw=line)
            buffer = buffer[newline_idx + 1 :]


def _tail_marker_prefix(buffer: str, marker: str) -> int:
    """Length of the longest suffix of ``buffer`` that is a prefix of ``marker``."""
    max_check = min(len(buffer), len(marker) - 1, _CARRY_MAX)
    for n in range(max_check, 0, -1):
        if marker.startswith(buffer[-n:]):
            return n
    return 0


# ---------------------------------------------------------------------------
# Op parsing + validation
# ---------------------------------------------------------------------------


_ALLOWED_OPS: Set[str] = {"add", "replace", "remove"}


@dataclass
class OpResult:
    """Outcome of parsing + validating one JSONL line.

    Exactly one of ``op`` / ``error`` is set.
    """

    op: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    # Set when the line was healed — describes what changed (for telemetry).
    healed_notes: List[str] = field(default_factory=list)


def parse_op_line(line: str, *, allowlist: Optional[Set[str]] = None) -> OpResult:
    """Parse one JSONL line into a structured op dict; validate shape.

    Returns ``OpResult.op`` populated when the line is a well-formed op
    addressing a known catalog type, with props that pass the type's
    Pydantic schema. Otherwise ``OpResult.error`` carries a short reason
    for telemetry.

    This is the deterministic gate — the healer (S1.2) runs *before* this
    on raw lines to repair common mistakes (missing currency, stringly
    money, etc.). Anything still broken at this layer is logged and dropped.

    ``allowlist`` is the per-template resolved primitive allowlist (see
    ``ui_catalog.resolve_allowlist`` + ``UiCatalogConfig``). When set, an
    ``add`` op whose type is known to the catalog but NOT in this allowlist
    drops with reason ``primitive_disabled:<type>`` — distinct from
    ``unknown_type``, so telemetry separates "LLM hallucinated" from
    "merchant turned off". When ``None``, no template-level filtering
    applies (all known catalog types accepted).
    """
    try:
        op = json.loads(line)
    except json.JSONDecodeError as exc:
        return OpResult(error=f"malformed_json: {exc.msg}")

    if not isinstance(op, dict):
        return OpResult(error="op_not_object")

    op_kind = op.get("op")
    if op_kind not in _ALLOWED_OPS:
        return OpResult(error=f"unknown_op:{op_kind!r}")

    op_id = op.get("id")
    if not isinstance(op_id, str) or not op_id:
        return OpResult(error="missing_or_empty_id")

    if op_kind == "remove":
        # Remove is just (op, id) — no type/props/parent required.
        extra = set(op.keys()) - {"op", "id"}
        if extra:
            for k in extra:
                op.pop(k, None)
        return OpResult(op=op)

    # add / replace require either a type (add) or props (replace).
    if op_kind == "add":
        type_name = op.get("type")
        if not isinstance(type_name, str) or not type_name:
            return OpResult(error="add_missing_type")
        if not is_known_type(type_name):
            return OpResult(error=f"unknown_type:{type_name!r}")
        # Template-level enable check — distinct telemetry from unknown_type.
        if allowlist is not None and type_name not in allowlist:
            return OpResult(error=f"primitive_disabled:{type_name}")
        parent = op.get("parent")
        # Root op has id == "root" by convention; everyone else needs parent.
        if op_id != "root" and (not isinstance(parent, str) or not parent):
            return OpResult(error="missing_parent")

    elif op_kind == "replace":
        # Type may be present (info only) but we don't require it; replace
        # carries new props only. Look up existing node's type from session
        # state at apply-time on the widget side.
        if "props" not in op:
            return OpResult(error="replace_missing_props")

    # Validate props (if any) against catalog.
    props = op.get("props") or {}
    if not isinstance(props, dict):
        return OpResult(error="props_not_object")

    # On `add`, we know the type. On `replace` without type, props are
    # weakly validated (must be an object); strong validation against the
    # existing node's type happens on the widget side, where the tree
    # state is authoritative.
    if op_kind == "add":
        try:
            validated = validate_props(op["type"], props)
        except Exception as exc:  # ValidationError + defensive
            return OpResult(error=f"props_validation_failed: {type(exc).__name__}")
        # Replace props with the model_dump so downstream callers see
        # normalized values (enum → string, HttpUrl → str, defaults filled).
        op["props"] = validated.model_dump(exclude_none=True, mode="json")

    return OpResult(op=op)


# ---------------------------------------------------------------------------
# SSE event factories
# ---------------------------------------------------------------------------


def ui_op_event(op: Dict[str, Any]) -> SSEEvent:
    """SSE event carrying a single applied op."""
    return SSEEvent(event="ui_op", data={"op": op})


def healer_applied_event(line: str, note: str) -> SSEEvent:
    """Observability event — fired when the healer fixed an incoming line.

    Carries the original ``line`` (truncated) and a short ``note`` describing
    the rule that fired. Widget ignores; server-side telemetry consumes.
    """
    truncated = line if len(line) <= 200 else line[:200] + "…"
    return SSEEvent(event="healer_applied", data={"raw": truncated, "note": note})


def ui_op_dropped_event(line: str, reason: str) -> SSEEvent:
    """Observability event — fired when a line failed validation and was
    dropped. Widget ignores."""
    truncated = line if len(line) <= 200 else line[:200] + "…"
    return SSEEvent(event="ui_op_dropped", data={"raw": truncated, "reason": reason})


# ---------------------------------------------------------------------------
# Process one JSONL line → SSEEvent(s)
# ---------------------------------------------------------------------------


HealerFn = Callable[[str, Dict[str, Any]], "HealerResult"]


@dataclass
class HealerResult:
    """Output of a healer rule pass. ``line`` is the (possibly rewritten)
    JSONL line; ``notes`` is the list of rule names that fired (for
    telemetry); ``drop`` is True when the line should be silently dropped
    (e.g. unknown type that the healer cannot rescue)."""

    line: Optional[str]
    notes: List[str] = field(default_factory=list)
    drop: bool = False


def process_op_line(
    raw_line: str,
    *,
    session_state: Optional[Dict[str, Any]] = None,
    healer: Optional[HealerFn] = None,
    known_ids: Optional[Set[str]] = None,
    allowlist: Optional[Set[str]] = None,
) -> List[SSEEvent]:
    """Run one raw JSONL line through (healer →) parse → validate → SSE.

    Returns a list of SSE events (0 to ~3) to forward to the client:
      * ``healer_applied`` — one per applied healer note (telemetry)
      * ``ui_op_dropped`` — when validation fails (telemetry)
      * ``ui_op`` — the validated op, ready for the widget

    ``known_ids`` lets the caller scope duplicate-id detection across the
    full session UI tree. The healer mutates it (adding/removing ids) so
    the caller should keep a single set across all lines in a turn.

    ``allowlist`` is the resolved-per-template primitive allowlist. Ops
    targeting types not in the allowlist drop with reason
    ``primitive_disabled:<type>`` — see ``parse_op_line``. When ``None``,
    no template-level filtering applies.
    """
    events: List[SSEEvent] = []
    line = raw_line

    if healer is not None:
        result = healer(line, session_state or {})
        for note in result.notes:
            events.append(healer_applied_event(line, note))
        if result.drop:
            return events
        if result.line is not None:
            line = result.line

    parsed = parse_op_line(line, allowlist=allowlist)
    if parsed.error:
        events.append(ui_op_dropped_event(line, parsed.error))
        return events

    op = parsed.op  # type: ignore[assignment]
    if known_ids is not None and op is not None:
        if op["op"] == "add":
            known_ids.add(op["id"])
        elif op["op"] == "remove":
            known_ids.discard(op["id"])

    events.append(ui_op_event(op))  # type: ignore[arg-type]
    return events


_UI_STREAM_MARKER_RE = re.compile(r"<ui_stream>.*?</ui_stream>", re.DOTALL)


def strip_ui_stream_markers(text: str) -> str:
    """Remove every ``<ui_stream>…</ui_stream>`` block from ``text``.

    Used at persistence time so saved assistant prose never carries SpecStream
    JSONL forward into future turns — the LLM would otherwise read its own
    prior op stream as if it were chat prose.
    """
    if not text:
        return text
    return _UI_STREAM_MARKER_RE.sub("", text)


_SUMMARY_TITLE_CAP = 80
_SUMMARY_TILE_CAP = 8
_SUMMARY_URL_CAP = 60


def summarize_ui_ops(ops: List[Dict[str, Any]]) -> str:
    """Produce a compact 1-line summary of UI ops emitted in a turn.

    The chat history strips ``<ui_stream>`` blocks before replay (so the LLM
    doesn't re-read raw JSONL), which leaves it blind to what tiles, prices,
    and actions the shopper actually saw. This summary restores a thin
    memory — enough for the LLM to disambiguate referents like "the green
    one" or "the bigger size" on follow-up turns, without paying the full
    op-stream token cost.

    Format: ``[ui rendered: 4 Tile(s): 'Title A', 'Title B'; 1 Handoff(s) → https://...]``
    Returns ``""`` if nothing user-facing was added.
    """
    if not ops:
        return ""

    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for op in ops:
        if op.get("op") != "add":
            continue
        type_name = op.get("type") or "?"
        by_type.setdefault(type_name, []).append(op.get("props") or {})

    parts: List[str] = []

    tiles = by_type.pop("Tile", None)
    if tiles:
        titles = [
            repr(str(p.get("title") or "?")[:_SUMMARY_TITLE_CAP])
            for p in tiles[:_SUMMARY_TILE_CAP]
        ]
        tail = (
            f" (+{len(tiles) - _SUMMARY_TILE_CAP} more)"
            if len(tiles) > _SUMMARY_TILE_CAP
            else ""
        )
        parts.append(f"{len(tiles)} Tile(s): {', '.join(titles)}{tail}")

    handoffs = by_type.pop("Handoff", None)
    if handoffs:
        urls = sorted({str(p.get("url") or "?")[:_SUMMARY_URL_CAP] for p in handoffs})
        parts.append(f"{len(handoffs)} Handoff(s) → {', '.join(urls)}")

    # Remaining primitives (Card, Carousel, Table, Message, etc.) — count only.
    # Layout containers like Stack/Row are skipped from the summary; they
    # describe arrangement, not content the shopper meaningfully sees.
    _LAYOUT_ONLY = {"Stack", "Row"}
    for type_name in sorted(by_type):
        if type_name in _LAYOUT_ONLY:
            continue
        items = by_type[type_name]
        parts.append(f"{len(items)} {type_name}")

    if not parts:
        return ""
    return "[ui rendered: " + "; ".join(parts) + "]"


__all__ = [
    "UiStreamExtractor",
    "TextOut",
    "JsonlOpLine",
    "YieldItem",
    "OpResult",
    "HealerResult",
    "HealerFn",
    "parse_op_line",
    "process_op_line",
    "ui_op_event",
    "healer_applied_event",
    "ui_op_dropped_event",
    "strip_ui_stream_markers",
    "summarize_ui_ops",
]
