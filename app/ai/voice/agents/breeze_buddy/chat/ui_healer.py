"""In-stream healer — deterministic rule layer over raw JSONL ops.

Sits between the LLM's emission and the parser/validator. Fixes the few
mechanical mistakes the LLM repeatedly makes (duplicate ids, unknown
prop keys, near-miss prop names) without burning a second LLM call.
<250ms latency per Vercel v0's "LLM Suspense" pattern.

Each rule is a pure function ``(op_dict, ctx) → Optional[op_dict]`` plus
a tag string for telemetry. The dispatcher applies rules in order; the
first rule to mutate the op annotates the ``notes`` list, then the next
rule sees the mutated op. ``drop=True`` short-circuits the pipeline.

The healer is intentionally code-defined (not template-configurable) —
mechanical fixes don't vary by merchant. See SCALE_ROADMAP.md §"Open
questions / Healer rule-set governance".
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from app.ai.voice.agents.breeze_buddy.chat.ui_stream import HealerResult
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    QUICK_REPLIES_MAX_ITEMS,
    UI_CATALOG,
    is_known_type,
)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Healer context — what each rule may inspect/mutate beyond the op
# ---------------------------------------------------------------------------


class HealerContext:
    """Per-line healer ctx. Carries session state (read-only for rules) and
    the running ``known_ids`` set (mutated by rules). Constructed by the
    chat agent at the start of each turn and passed to ``heal_op``."""

    def __init__(
        self,
        *,
        session_data: Dict[str, Any],
        known_ids: Set[str],
    ) -> None:
        self.session_data = session_data
        self.known_ids = known_ids


# ---------------------------------------------------------------------------
# Individual rules — each returns (mutated_op, note_or_None)
# ---------------------------------------------------------------------------


# Per-primitive prop-name aliases the LLM frequently confuses. Applied
# BEFORE the unknown-props strip so a Tag.label → Tag.text rename rescues
# an op that would otherwise drop. Keys are (type, alias) → canonical name.
# Empirically derived from real-traffic emissions — extend as new misses
# appear in `ui_op_dropped` telemetry.
_PROP_ALIASES: Dict[Tuple[str, str], str] = {
    ("Tag", "label"): "text",
    ("Tag", "name"): "text",
    ("Tag", "value"): "text",
    ("Text", "content"): "text",  # legacy DSL prop name
    ("Text", "value"): "text",
    ("Button", "title"): "label",
    ("Button", "text"): "label",
    ("Image", "url"): "src",
    ("Image", "image"): "src",
    ("Image", "title"): "alt",
    ("CardHeader", "heading"): "title",
    ("CardHeader", "header"): "title",
}


def _rule_rename_prop_aliases(
    op: Dict[str, Any], ctx: HealerContext
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Rename common alias props onto their catalog-canonical names BEFORE
    the unknown-props strip. Saves Tag.label → Tag.text etc. — the LLM
    consistently reaches for these synonyms across runs, and the
    strip-then-validate path would otherwise drop the whole op."""
    if op.get("op") != "add":
        return op, None
    type_name = op.get("type")
    if not isinstance(type_name, str) or not is_known_type(type_name):
        return op, None
    props = op.get("props")
    if not isinstance(props, dict):
        return op, None
    schema = UI_CATALOG[type_name]
    allowed = set(schema.model_fields.keys())
    renames: List[str] = []
    for alias_name in list(props.keys()):
        canonical = _PROP_ALIASES.get((type_name, alias_name))
        if not canonical or canonical not in allowed or alias_name in allowed:
            continue
        # Skip rename if the canonical key is already set — LLM intent wins.
        if canonical in props and props[canonical] is not None:
            continue
        props[canonical] = props.pop(alias_name)
        renames.append(f"{alias_name}->{canonical}")
    if not renames:
        return op, None
    op["props"] = props
    return op, f"renamed_alias_props:{type_name}:{','.join(renames)}"


def _rule_strip_unknown_props(
    op: Dict[str, Any], ctx: HealerContext
) -> Tuple[Dict[str, Any], Optional[str]]:
    """For ``add`` ops: drop props that aren't in the catalog schema."""
    if op.get("op") != "add":
        return op, None
    type_name = op.get("type")
    if not isinstance(type_name, str) or not is_known_type(type_name):
        return op, None
    schema = UI_CATALOG[type_name]
    allowed = set(schema.model_fields.keys())
    props = op.get("props") or {}
    if not isinstance(props, dict):
        return op, None
    extras = [k for k in props if k not in allowed]
    if not extras:
        return op, None
    for k in extras:
        props.pop(k, None)
    op["props"] = props
    return op, f"stripped_unknown_props:{','.join(extras)}"


def _rule_button_default_label(
    op: Dict[str, Any], ctx: HealerContext
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Button with an action but no label → default ``"Continue"``."""
    if op.get("op") != "add" or op.get("type") != "Button":
        return op, None
    props = op.get("props")
    if not isinstance(props, dict):
        return op, None
    if props.get("label"):
        return op, None
    if "action" not in props:
        return op, None
    props["label"] = "Continue"
    op["props"] = props
    return op, "button_default_label"


def _rule_tag_flatten_array_text(
    op: Dict[str, Any], ctx: HealerContext
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Tag.text emitted as a JSON-array string → flatten to ``"a, b, c"``."""
    if op.get("op") != "add" or op.get("type") != "Tag":
        return op, None
    props = op.get("props")
    if not isinstance(props, dict):
        return op, None
    text = props.get("text")
    if not isinstance(text, str):
        return op, None
    stripped = text.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        return op, None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return op, None
    if not isinstance(parsed, list):
        return op, None
    flat = ", ".join(str(x) for x in parsed if x is not None)
    if not flat:
        return op, None
    props["text"] = flat
    op["props"] = props
    return op, "tag_flattened_array_text"


def _rule_clamp_quickreplies_items(
    op: Dict[str, Any], ctx: HealerContext
) -> Tuple[Dict[str, Any], Optional[str]]:
    """``QuickReplies`` over the item cap → truncate to the cap instead of
    letting the whole row drop on ``max_length``. The LLM occasionally emits
    more pills than allowed (e.g. every category in a storefront with more
    categories than the cap); showing the first N beats showing nothing.
    ``min_length`` underflow is left for the validator — the healer can't
    invent options."""
    if op.get("op") != "add" or op.get("type") != "QuickReplies":
        return op, None
    props = op.get("props")
    if not isinstance(props, dict):
        return op, None
    items = props.get("items")
    if not isinstance(items, list) or len(items) <= QUICK_REPLIES_MAX_ITEMS:
        return op, None
    dropped = len(items) - QUICK_REPLIES_MAX_ITEMS
    props["items"] = items[:QUICK_REPLIES_MAX_ITEMS]
    op["props"] = props
    return op, f"clamped_quickreplies_items:-{dropped}"


# Every ``url``/``src`` prop reachable here is a pydantic ``HttpUrl`` field —
# Handoff.url, Image.src, Icon.url, OpenUrlAction.url (nested in *.action),
# TileMedia.src (nested in *.media). The lone str-typed exception, ``SideEffect.url``
# (deliberately same-origin-relative), is excluded by the type guard in the rule
# below so its relative paths (e.g. ``cart.js``) survive untouched. Anything
# already carrying a scheme (http:, https:, mailto:, tel:) is left alone; a bare
# host like ``shop.com/cart`` — which ``HttpUrl`` rejects as "relative URL without
# a base" and drops the op — gets an ``https://`` prefix. Host-less relatives
# (leading ``/`` ``#`` ``?``) have no host to attach a scheme to → left for the
# validator.
_URL_PROP_KEYS = frozenset({"url", "src"})
_HAS_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _coerce_url_string(value: Any) -> Tuple[Any, bool]:
    if not isinstance(value, str):
        return value, False
    s = value.strip()
    if not s or _HAS_SCHEME_RE.match(s):
        return value, False
    if s.startswith("//"):  # protocol-relative → pin to https
        return "https:" + s, True
    if s[0] in "/#?":  # host-less relative path / anchor / query — can't fix
        return value, False
    head = s.split("/", 1)[0].split("?", 1)[0]
    if "." in head and " " not in head:  # looks like a domain
        return "https://" + s, True
    return value, False


def _walk_coerce_urls(node: Any) -> int:
    coerced = 0
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if k in _URL_PROP_KEYS:
                new_v, changed = _coerce_url_string(v)
                if changed:
                    node[k] = new_v
                    coerced += 1
            else:
                coerced += _walk_coerce_urls(v)
    elif isinstance(node, list):
        for item in node:
            coerced += _walk_coerce_urls(item)
    return coerced


def _rule_coerce_scheme_less_urls(
    op: Dict[str, Any], ctx: HealerContext
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Prepend ``https://`` to scheme-less host URLs in ``url`` / ``src`` props
    (including nested action / media). Rescues the checkout ``Handoff`` and any
    bare-domain link the LLM emits without a scheme — pydantic ``HttpUrl`` would
    otherwise reject it and the op would drop.

    Scoped to ``add`` ops (whose ``type`` we can trust) and skips ``SideEffect``,
    whose ``url`` is a deliberately same-origin-relative ``str`` (not ``HttpUrl``)
    — coercing it would corrupt a valid relative path like ``cart.js``."""
    if op.get("op") != "add" or op.get("type") == "SideEffect":
        return op, None
    props = op.get("props")
    if not isinstance(props, dict):
        return op, None
    coerced = _walk_coerce_urls(props)
    if not coerced:
        return op, None
    op["props"] = props
    return op, f"coerced_scheme_less_url:{coerced}"


def _rule_dedupe_id(
    op: Dict[str, Any], ctx: HealerContext
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Duplicate ``id`` in the same session → rename to ``id__2`` / ``id__3``."""
    if op.get("op") != "add":
        return op, None
    op_id = op.get("id")
    if not isinstance(op_id, str) or not op_id:
        return op, None
    if op_id not in ctx.known_ids:
        return op, None
    n = 2
    while f"{op_id}__{n}" in ctx.known_ids:
        n += 1
    new_id = f"{op_id}__{n}"
    op["id"] = new_id
    return op, f"renamed_duplicate_id:{op_id}->{new_id}"


def _rule_drop_unknown_type(
    op: Dict[str, Any], ctx: HealerContext
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``add`` op with an unknown ``type`` → drop (healer can't invent one)."""
    if op.get("op") != "add":
        return op, None
    type_name = op.get("type")
    if isinstance(type_name, str) and not is_known_type(type_name):
        return None, f"dropped_unknown_type:{type_name!r}"
    return op, None


def _rule_drop_orphan_add(
    op: Dict[str, Any], ctx: HealerContext
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Non-root ``add`` op missing ``parent`` → drop. Refuse to guess the
    intended parent — that's a worse failure mode than a missing card."""
    if op.get("op") != "add":
        return op, None
    op_id = op.get("id")
    if op_id == "root":
        return op, None
    parent = op.get("parent")
    if not isinstance(parent, str) or not parent:
        return None, "dropped_orphan_add"
    return op, None


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


# Order matters: rename aliases first (so Tag.label → Tag.text doesn't
# get stripped); then strip remaining unknowns; then per-primitive
# coercions (incl. QuickReplies item-clamp and scheme-less URL repair, which
# run on the cleaned props); then dedupe last (the rename can mutate
# id-bearing props in theory). Drops run after, gating on the cleaned-up shape.
_TRANSFORM_RULES: List[
    Callable[[Dict[str, Any], HealerContext], Tuple[Dict[str, Any], Optional[str]]]
] = [
    _rule_rename_prop_aliases,
    _rule_strip_unknown_props,
    _rule_button_default_label,
    _rule_tag_flatten_array_text,
    _rule_clamp_quickreplies_items,
    _rule_coerce_scheme_less_urls,
    _rule_dedupe_id,
]


_DROP_RULES: List[
    Callable[
        [Dict[str, Any], HealerContext], Tuple[Optional[Dict[str, Any]], Optional[str]]
    ]
] = [
    _rule_drop_unknown_type,
    _rule_drop_orphan_add,
]


def heal_op_line(raw_line: str, ctx: HealerContext) -> HealerResult:
    """Apply healer rules to one JSONL line.

    1. Parse the raw line as JSON (skip → drop on malformed).
    2. Apply transform rules in order; collect notes per applied rule.
    3. Apply drop rules; if any fires, return ``drop=True``.
    4. Serialize back to JSONL and return.

    The returned line is *guaranteed* to be valid JSON (because we just
    serialized it), but is NOT guaranteed to pass catalog validation —
    that's the parser/validator's job downstream.
    """
    notes: List[str] = []

    try:
        op = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        return HealerResult(
            line=None,
            notes=[f"dropped_malformed_json:{exc.msg}"],
            drop=True,
        )

    if not isinstance(op, dict):
        return HealerResult(line=None, notes=["dropped_op_not_object"], drop=True)

    for rule in _TRANSFORM_RULES:
        op, note = rule(op, ctx)
        if note:
            notes.append(note)

    for drop_rule in _DROP_RULES:
        op_or_none, note = drop_rule(op, ctx)
        if op_or_none is None:
            if note:
                notes.append(note)
            return HealerResult(line=None, notes=notes, drop=True)
        op = op_or_none

    return HealerResult(line=json.dumps(op), notes=notes, drop=False)


def make_healer_fn(ctx: HealerContext):
    """Closure adaptor — returns a ``HealerFn`` (the shape expected by
    :func:`process_op_line`)."""

    def _healer(raw: str, _session_state: Dict[str, Any]) -> HealerResult:
        return heal_op_line(raw, ctx)

    return _healer


__all__ = [
    "HealerContext",
    "heal_op_line",
    "make_healer_fn",
]
