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

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Literal, Optional, Set, Union

from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    is_known_type,
    validate_props,
)

_log = logging.getLogger(__name__)

_OPEN = "<ui_stream>"
_CLOSE = "</ui_stream>"
_CARRY_MAX = max(len(_OPEN), len(_CLOSE)) - 1

# Compact op markers ("+" add / "~" replace / "-" remove) and the verbose
# ``{"op": ...}`` verbs. Used to recognize a bare SpecStream op-line the LLM
# emitted WITHOUT the ``<ui_stream>`` wrapper, so it can be recovered into the
# op path instead of leaking to the user as raw JSON. See ``_is_op_line``.
_OP_MARKERS: Set[str] = {"+", "~", "-"}
_OP_VERBS: Set[str] = {"add", "replace", "remove"}


def _is_op_line(text: str) -> bool:
    """True iff ``text`` is a bare SpecStream op-line.

    Conservative on purpose: the line must parse to a JSON **object** carrying
    an op marker (compact ``{"+"|"~"|"-": …}`` or verbose
    ``{"op": "add"|"replace"|"remove", …}``). Ordinary prose — even prose that
    contains braces or a stray JSON value — never matches, so nothing that
    isn't genuinely UI gets diverted out of the visible reply.
    """
    if not text.startswith("{"):
        return False
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return False
    if not isinstance(obj, dict):
        return False
    if _OP_MARKERS & obj.keys():
        return True
    return obj.get("op") in _OP_VERBS


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
        # OUTSIDE accumulator: holds only a pending line that STARTS with "{"
        # (a possible bare op-line) until a newline decides op-vs-prose. Prose
        # never sits here — it streams immediately — so smoothness is preserved.
        self._out_line_buf: str = ""

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
            # A held partial op-line (no trailing newline) + any marker-prefix
            # carry. Route through the op recogniser so a wrapper-less final op
            # is recovered rather than leaked; anything else falls back to prose.
            leftover = self._out_line_buf + self._carry
            self._out_line_buf = ""
            self._carry = ""
            if leftover:
                yield from self._emit_outside_line(leftover)
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
                    # Definite boundary before the open marker — flush any held
                    # partial op-line too, then switch INSIDE.
                    yield from self._emit_outside(buffer[:idx], flush_partial=True)
                    buffer = buffer[idx + len(_OPEN) :]
                    self._state = "INSIDE"
                    continue
                hold = _tail_marker_prefix(buffer, _OPEN)
                if hold:
                    body = buffer[:-hold] if len(buffer) > hold else ""
                    yield from self._emit_outside(body, flush_partial=False)
                    self._carry = buffer[-hold:]
                else:
                    yield from self._emit_outside(buffer, flush_partial=False)
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

    def _emit_outside(self, text: str, flush_partial: bool) -> Iterator[YieldItem]:
        """Emit OUTSIDE (non-block) text, recovering a bare op-line the LLM
        forgot to wrap in ``<ui_stream>``.

        Prose streams immediately for smoothness; only a line that *starts*
        with ``{`` is held (until its newline) so we can decide op-vs-prose. A
        recovered op-line is yielded as ``JsonlOpLine`` — the same item a
        wrapped line produces — so it flows through the identical heal/render
        path and shows as UI instead of leaking as raw JSON. ``flush_partial``
        forces a held partial line out (used when a definite ``<ui_stream>``
        boundary follows).
        """
        self._out_line_buf += text
        while self._out_line_buf:
            starts_brace = self._out_line_buf.lstrip().startswith("{")
            newline_idx = self._out_line_buf.find("\n")
            if not starts_brace:
                # Definitely prose — stream up to the newline, or all of it.
                if newline_idx == -1:
                    yield TextOut(value=self._out_line_buf)
                    self._out_line_buf = ""
                    return
                yield TextOut(value=self._out_line_buf[: newline_idx + 1])
                self._out_line_buf = self._out_line_buf[newline_idx + 1 :]
                continue
            # Might be a bare op-line — need the whole line to know.
            if newline_idx == -1:
                if flush_partial:
                    yield from self._emit_outside_line(self._out_line_buf)
                    self._out_line_buf = ""
                return
            line = self._out_line_buf[: newline_idx + 1]
            self._out_line_buf = self._out_line_buf[newline_idx + 1 :]
            yield from self._emit_outside_line(line)

    def _emit_outside_line(self, line: str) -> Iterator[YieldItem]:
        """Classify one OUTSIDE line: a bare op-line is recovered as a
        ``JsonlOpLine`` (so it renders); anything else is prose (``TextOut``)."""
        stripped = line.strip()
        if _is_op_line(stripped):
            yield JsonlOpLine(raw=stripped)
        elif line:
            yield TextOut(value=line)


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


# ---------------------------------------------------------------------------
# Compact wire form (A3) — a terse shorthand the LLM emits, expanded
# server-side to the canonical op shape *before* healing/validation. See
# docs/widget/UI_FAST_RELIABLE_GENERIC_PLAN.md. Pure encoding change: the
# canonical ops downstream — and the persisted ui_blocks the widget reads —
# are unchanged. Both forms are accepted, so partial LLM adoption is always
# safe; a malformed compact op falls through to the normal dropped path.
#
#   add:     {"+":"<id>:<Type>@<parent>", <prop>:<val>, ...}   (root drops "@<parent>")
#   replace: {"~":"<id>", <prop>:<val>, ...}
#   remove:  {"-":"<id>"}
#   body row shorthand: {"kv":["Price","₹699"]} -> {"kind":"key_value","key":"Price","value":"₹699"}
# ---------------------------------------------------------------------------

_COMPACT_ADD_RE = re.compile(r"^\s*([^:@\s]+)\s*:\s*([^:@\s]+)\s*(?:@\s*(.+?))?\s*$")


def _expand_body_shorthand(props: Dict[str, Any]) -> Dict[str, Any]:
    """Expand ``{"kv":[k, v]}`` body items into canonical ``key_value`` items.

    Only touches a list-valued ``body``; any item already in canonical
    ``{"kind": ...}`` form (or any other shape) is left untouched.
    """
    body = props.get("body")
    if not isinstance(body, list):
        return props
    new_body: List[Any] = []
    changed = False
    for item in body:
        kv = item.get("kv") if isinstance(item, dict) else None
        if isinstance(kv, list) and len(kv) == 2:
            new_body.append({"kind": "key_value", "key": kv[0], "value": kv[1]})
            changed = True
        else:
            new_body.append(item)
    if not changed:
        return props
    return {**props, "body": new_body}


def expand_compact_op(op: Dict[str, Any]) -> Dict[str, Any]:
    """Expand one compact-form op dict to canonical shape.

    Returns the input unchanged when it already carries an ``op`` key
    (canonical) or is not a recognised compact form — so the two forms
    coexist and a malformed compact op flows on to the normal parser, which
    drops it with telemetry.
    """
    if not isinstance(op, dict) or "op" in op:
        return op

    spec = op.get("+")
    if isinstance(spec, str):
        m = _COMPACT_ADD_RE.match(spec)
        if not m:
            return op
        out: Dict[str, Any] = {"op": "add", "id": m.group(1), "type": m.group(2)}
        if m.group(3):
            out["parent"] = m.group(3)
        props = {k: v for k, v in op.items() if k != "+"}
        # `repeat` is a structural directive (A1), not a visual prop — hoist it
        # to the op level so the repeat expander sees it.
        if "repeat" in props:
            out["repeat"] = props.pop("repeat")
        out["props"] = _expand_body_shorthand(props)
        return out

    rid = op.get("~")
    if isinstance(rid, str):
        return {
            "op": "replace",
            "id": rid,
            "props": _expand_body_shorthand({k: v for k, v in op.items() if k != "~"}),
        }

    did = op.get("-")
    if isinstance(did, str):
        return {"op": "remove", "id": did}

    return op


def expand_compact_line(raw_line: str) -> str:
    """Expand a compact-form JSONL line to canonical JSONL.

    Pass-through (returns ``raw_line`` verbatim) for canonical lines and for
    anything that doesn't parse as a JSON object — the downstream healer /
    parser handle those exactly as before.
    """
    try:
        op = json.loads(raw_line)
    except (json.JSONDecodeError, ValueError):
        return raw_line
    if not isinstance(op, dict):
        return raw_line
    expanded = expand_compact_op(op)
    if expanded is op:
        return raw_line
    return json.dumps(expanded, ensure_ascii=False)


# ---------------------------------------------------------------------------
# A1 — data/structure split (repeat + $item). The LLM emits ONE element with a
# ``repeat`` directive carrying an inline data array; the server instantiates
# the element once per row, binding ``{"$item":"<path>"}`` placeholders to that
# row's value, and emits N canonical flat ops. The widget is unchanged (it gets
# the same flat ops). Domain-blind: the engine only does "template x array -> N
# ops"; what to show and which data live in the LLM-authored element + array, so
# emergent per-item choices (e.g. binding the red variant's image) survive.
# See docs/widget/UI_FAST_RELIABLE_GENERIC_PLAN.md.
#
#   {"op":"add","id":"tile","type":"Tile","parent":"root",
#    "repeat":{"items":[{...},{...}],"key":"id"},
#    "props":{"title":{"$item":"title"}, "media":{"src":{"$item":"image"}}}}
# ---------------------------------------------------------------------------


def _lookup_item_path(item: Any, path: str) -> Any:
    """Resolve a dotted path (``a.b.c``) against one repeat row. A missing key
    or a non-dict mid-path yields ``None`` (the prop is then dropped by
    ``exclude_none`` at validation)."""
    cur = item
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _resolve_item_refs(value: Any, item: Any) -> Any:
    """Recursively replace ``{"$item":"<path>"}`` placeholders with the value
    at that path in ``item``. Everything else passes through unchanged, so a
    template can freely mix bound and literal props."""
    if isinstance(value, dict):
        ref = value.get("$item")
        if len(value) == 1 and isinstance(ref, str):
            return _lookup_item_path(item, ref)
        return {k: _resolve_item_refs(v, item) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_item_refs(v, item) for v in value]
    return value


def expand_repeat_line(line: str) -> List[str]:
    """Expand a ``repeat`` template line into N canonical instance lines.

    A line without ``repeat`` returns ``[line]`` unchanged (the common case).
    A well-formed ``repeat`` returns one canonical op line per data row, with a
    unique id derived from ``key`` (or the row index) and every ``{"$item":...}``
    placeholder resolved. A ``repeat`` whose ``items`` isn't a list returns
    ``[]`` — the bare template is never emitted, so it can't leak to the widget.
    """
    try:
        op = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return [line]
    if not isinstance(op, dict) or "repeat" not in op:
        return [line]

    rep = op.get("repeat")
    items = rep.get("items") if isinstance(rep, dict) else None
    if not isinstance(items, list):
        return []
    key = rep.get("key") if isinstance(rep, dict) else None
    base = {k: v for k, v in op.items() if k != "repeat"}
    tpl_id = base.get("id") or "item"
    tpl_props = base.get("props")

    out: List[str] = []
    for idx, item in enumerate(items):
        inst = dict(base)
        suffix: Optional[str] = None
        if isinstance(key, str) and isinstance(item, dict):
            kv = item.get(key)
            if kv is not None:
                suffix = str(kv)
        inst["id"] = f"{tpl_id}-{suffix if suffix is not None else idx}"
        if isinstance(tpl_props, (dict, list)):
            inst["props"] = _resolve_item_refs(tpl_props, item)
        out.append(json.dumps(inst, ensure_ascii=False))
    return out


def _repeat_with_nonlist_items(line: str) -> bool:
    """True when ``line`` is a ``repeat`` op whose ``items`` isn't a list.

    Distinguishes a *malformed* repeat (which ``expand_repeat_line`` drops to
    ``[]``) from a legitimately *empty* ``items: []`` (also ``[]`` but valid,
    nothing to render). Lets ``process_op_line`` surface the malformed case as
    an observable ``ui_op_dropped`` instead of a silent no-op.
    """
    try:
        op = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(op, dict) or "repeat" not in op:
        return False
    rep = op.get("repeat")
    items = rep.get("items") if isinstance(rep, dict) else None
    return not isinstance(items, list)


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
        except ValidationError as exc:
            # Structural detail only — field paths + pydantic error kinds,
            # never input values (privacy contract shared with
            # ``ui_op_dropped_event``). This is what makes drop telemetry
            # actionable: "Button:action.OpenUrlAction.url:url_scheme"
            # diagnoses itself; a bare "ValidationError" does not.
            details = ";".join(
                f"{'.'.join(str(p) for p in err.get('loc', ()))}:{err.get('type')}"
                for err in exc.errors()[:5]
            )
            return OpResult(error=f"props_validation_failed:{op['type']}:{details}")
        except Exception as exc:  # defensive
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


def _op_signature(line: str) -> Dict[str, Any]:
    """Structural-only descriptor of an op line for telemetry.

    After ``repeat`` expansion a line carries resolved per-row props (titles,
    prices, image URLs, …). Telemetry events must never forward that content,
    so we surface only the structural keys (``op``/``id``/``type`` — ids and
    type names, never payload), falling back to a short content hash so a
    malformed/unparseable line is still correlatable without exposing data.
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        obj = None
    if isinstance(obj, dict):
        sig = {
            k: obj[k]
            for k in ("op", "id", "type")
            if isinstance(obj.get(k), (str, int))
        }
        if sig:
            return sig
    digest = hashlib.sha1(line.encode("utf-8", "replace")).hexdigest()[:12]
    return {"hash": digest}


def healer_applied_event(line: str, note: str) -> SSEEvent:
    """Observability event — fired when the healer fixed an incoming line.

    Carries a structural-only op signature (never the resolved payload) and a
    short ``note`` describing the rule that fired. Widget ignores; server-side
    telemetry consumes.
    """
    return SSEEvent(
        event="healer_applied", data={"op": _op_signature(line), "note": note}
    )


# Cap on the raw dropped line carried in the event — a pathological repeat
# expansion can't bloat the SSE frame or the persisted drops row.
_DROP_RAW_MAX = 2000


def ui_op_dropped_event(line: str, reason: str) -> SSEEvent:
    """Observability event — fired when a line failed validation and was
    dropped. Widget ignores.

    Emits a structural-only op signature and the first line of ``reason`` —
    Pydantic errors echo ``input_value`` on later lines, so we drop those
    here too (defense-in-depth alongside the metrics layer's own truncation).

    ``raw`` carries the dropped line itself (capped) so the metrics observer
    can persist it beside the transcript (chat_turn_metrics.drops, migration
    041) — that row is what makes drops diagnosable from the dashboard
    without log archaeology. Same-session assistant-generated content only;
    it must never be copied into log lines.
    """
    first_reason = reason.split("\n", 1)[0][:200]
    return SSEEvent(
        event="ui_op_dropped",
        data={
            "op": _op_signature(line),
            "reason": first_reason,
            "raw": line[:_DROP_RAW_MAX],
        },
    )


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

    Returns a list of SSE events to forward to the client (0 to many — a
    ``repeat`` template fans out to one ``ui_op`` per data row):
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
    # A3 — expand the compact wire form to canonical; then A1 — fan a `repeat`
    # template out into one canonical line per data row. A plain op is a single
    # line; canonical/unparseable lines pass through untouched.
    compact_line = expand_compact_line(raw_line)
    expanded = expand_repeat_line(compact_line)
    # A malformed `repeat` (items not a list) expands to nothing. Surface it as
    # an observable drop rather than rendering silently — keeps the reliability
    # metrics honest. A legitimately empty `items: []` is not flagged.
    if not expanded and _repeat_with_nonlist_items(compact_line):
        events.append(ui_op_dropped_event(compact_line, "repeat_items_not_list"))
        return events
    for line in expanded:
        if healer is not None:
            result = healer(line, session_state or {})
            # A ``dropped_*`` note is the drop's reason, not a repair —
            # surface it as ``ui_op_dropped`` (below) rather than
            # ``healer_applied``, so healer drops land in the SAME funnel
            # counter as validator drops. Before this, healer drops were
            # invisible in ``ui_dropped`` — the badge under-counted.
            drop_note: Optional[str] = None
            for note in result.notes:
                if note.startswith("dropped_"):
                    drop_note = drop_note or note
                else:
                    events.append(healer_applied_event(line, note))
            if result.drop:
                events.append(ui_op_dropped_event(line, drop_note or "healer_drop"))
                continue
            if result.line is not None:
                line = result.line

        parsed = parse_op_line(line, allowlist=allowlist)
        if parsed.error:
            events.append(ui_op_dropped_event(line, parsed.error))
            continue

        op = parsed.op  # type: ignore[assignment]
        if known_ids is not None and op is not None:
            # Guarantee the conventional `root` container is anchored before any
            # block attaches to it. The widget anchors its per-message tree on
            # the first `add id="root"` and DROPS any add whose parent doesn't
            # exist. A turn that renders MULTIPLE top-level blocks (e.g. a cart
            # card AND a product carousel) can't make them all BE root, so the
            # model parents each to `root` with its own id — but may forget to
            # `add` root, which orphans the whole tree into a blank reply. Inject
            # a neutral Stack root so no block orphans; rescue a stray
            # `replace root` (model assuming a prior turn's root) into that
            # anchoring add. Domain-blind; a no-op once root exists, so normal
            # single-block renders (the block IS root) are untouched.
            if "root" not in known_ids:
                if op["op"] == "replace" and op["id"] == "root":
                    op = {
                        "op": "add",
                        "id": "root",
                        "type": "Stack",
                        "props": op.get("props") or {"gap": "md"},
                    }
                elif op["op"] == "add" and op.get("parent") == "root":
                    known_ids.add("root")
                    events.append(
                        ui_op_event(
                            {
                                "op": "add",
                                "id": "root",
                                "type": "Stack",
                                "props": {"gap": "md"},
                            }
                        )
                    )

            if op["op"] == "add":
                known_ids.add(str(op["id"]))
            elif op["op"] == "remove":
                known_ids.discard(str(op["id"]))

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
    "expand_compact_op",
    "expand_compact_line",
    "expand_repeat_line",
    "parse_op_line",
    "process_op_line",
    "ui_op_event",
    "healer_applied_event",
    "ui_op_dropped_event",
    "strip_ui_stream_markers",
    "summarize_ui_ops",
]
