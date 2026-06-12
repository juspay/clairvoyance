"""In-place response transforms.

Companion to ``response_filter.apply_response_schema`` — but where that one
*projects* (reshapes the response into a new dict via JMESPath), this one
*mutates* specific paths in-place while everything else passes through
untouched. The two are complementary, both declared in the template, both
applied at the tool handler before the result reaches the LLM.

Why we need it
--------------
Some upstream tools return slightly-wrong-shape data the LLM can't fix
cheaply — e.g. an integer needs unscaling by a power of ten, a nested
field needs flattening. We want the LLM to see one consistent shape.
Projecting the entire response just to scale two fields forces the
template author to enumerate every preserved key — fragile and verbose.
An in-place transform is the natural fit.

Channel-agnostic
----------------
Transforms are applied inside the tool handler closure (HTTP adapter +
MCP handler), which is the chokepoint both voice and chat dispatch
through. Nothing channel-specific to wire up.

Vertical-agnostic
-----------------
The registry ships only generic arithmetic / shape ops. Any vertical
(commerce, scheduling, finance, …) declares which transforms to apply
in its template — the engine itself knows nothing about money, carts,
appointments, or invoices.

Authoring shape
---------------
Each transform is ``{path, fn, args?}``:

    {
        "path": "products[*].price_range.min",
        "fn": "scale_by_exponent",
        "args": {"exponent": 2}
    }

Path syntax is the subset of JSON pointer-ish dotted notation we need:

    - ``a.b.c``       — descend dict keys
    - ``a[*].b``      — iterate every element of array ``a`` and descend ``b`` on each
    - ``a[*]``        — apply the transform to every element of ``a`` directly

JMESPath was considered for the path field but it's read-only (returns
copies, not references), so we'd have to walk the data Python-side anyway
to mutate. A small purpose-built walker is simpler and keeps the path
spec narrowly scoped to the in-place-mutation idiom.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from app.core.logger import logger

if TYPE_CHECKING:
    from app.ai.voice.agents.breeze_buddy.template.types import ResponseTransform

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

TransformFn = Callable[[Any, Dict[str, Any]], Any]
"""A transform takes the value at the matched path and an args dict (from the
template) and returns the new value to install at the same path."""

TRANSFORM_REGISTRY: Dict[str, TransformFn] = {}


def register_transform(name: str) -> Callable[[TransformFn], TransformFn]:
    """Register a transform fn under ``name``. Decorator form."""

    def _decorator(fn: TransformFn) -> TransformFn:
        if name in TRANSFORM_REGISTRY:
            logger.warning(
                f"[response_transform] re-registering '{name}' "
                f"(was: {TRANSFORM_REGISTRY[name].__name__})"
            )
        TRANSFORM_REGISTRY[name] = fn
        return fn

    return _decorator


# ---------------------------------------------------------------------------
# Path parsing + walker
# ---------------------------------------------------------------------------

# One path segment: a key name optionally followed by [*] (array iteration).
_SEGMENT_RE = re.compile(r"^([^.\[\]]+)(\[\*\])?$")


def _parse_path(path: str) -> List[Tuple[str, bool]]:
    """Parse a dotted path into (key, is_array) segments. Empty path → []."""
    if not path:
        return []
    segments: List[Tuple[str, bool]] = []
    for part in path.split("."):
        match = _SEGMENT_RE.match(part)
        if not match:
            raise ValueError(f"unsupported path segment {part!r} in {path!r}")
        segments.append((match.group(1), bool(match.group(2))))
    return segments


def _walk(
    data: Any,
    segments: List[Tuple[str, bool]],
    fn: TransformFn,
    args: Dict[str, Any],
) -> None:
    """Walk ``segments`` into ``data`` and apply ``fn`` at every leaf, mutating
    in place. Silently no-ops on shape mismatch (missing keys, non-list under
    [*], non-dict mid-path) so a partial response never blows up a turn.
    """
    if not segments or not isinstance(data, dict):
        return

    key, is_array = segments[0]
    rest = segments[1:]
    if key not in data:
        return
    target = data[key]

    if is_array:
        if not isinstance(target, list):
            return
        if rest:
            for item in target:
                _walk(item, rest, fn, args)
        else:
            data[key] = [fn(item, args) for item in target]
        return

    if rest:
        _walk(target, rest, fn, args)
    else:
        data[key] = fn(target, args)


def apply_response_transforms(
    data: Any,
    transforms: Optional[List["ResponseTransform"]],
) -> Any:
    """Run every transform in order against ``data``, mutating in place.

    Returns the (now-mutated) ``data`` for caller convenience. Unknown
    transform names are logged and skipped — a stale template never breaks
    a live turn.
    """
    if not transforms or not isinstance(data, (dict, list)):
        return data

    for rule in transforms:
        fn = TRANSFORM_REGISTRY.get(rule.fn)
        if fn is None:
            logger.warning(
                f"[response_transform] unknown fn {rule.fn!r} for path "
                f"{rule.path!r} — skipping"
            )
            continue

        try:
            segments = _parse_path(rule.path)
        except ValueError as e:
            logger.warning(f"[response_transform] {e} — skipping")
            continue

        # Allow the root-level case (empty path) — apply fn to the response root.
        if not segments:
            data = fn(data, rule.args)
            continue

        if isinstance(data, dict):
            _walk(data, segments, fn, rule.args)
        # Lists at the root with a path like "[*].…" aren't a use case yet —
        # MCP/HTTP responses we transform are dict-rooted. Skip silently if so.

    return data


# ---------------------------------------------------------------------------
# Built-in transforms — purely arithmetic / shape-level, no domain knowledge
# ---------------------------------------------------------------------------


@register_transform("scale_by_exponent")
def scale_by_exponent(value: Any, args: Dict[str, Any]) -> Any:
    """Normalise a numeric subfield to a display-formatted decimal string.

    Operates on a dict; reads the number at ``amount_field`` and writes a
    formatted display string back at the same key. Two input modes:

      * **Minor-unit input** (``int``, ``float``, or string with no ``.``/``,``):
        divides by ``10 ** exponent`` to convert to major units, then formats.
      * **Already-decimal input** (string containing ``.``): re-formats for
        display consistency (e.g. ``"1585.9"`` → ``"1,585.90"``) without
        re-scaling — the value is taken as-is in major units.

    Output is always a thousand-separated string with ``exponent`` decimal
    places, so all downstream display strings have consistent shape regardless
    of whether the upstream tool returned minor units or pre-formatted decimals.

    Args:
        amount_field — key holding the number (default: ``"amount"``)
        exponent     — non-negative integer exponent of 10 (default: ``2``).
                       For ISO 4217 currencies that's 2 for INR/USD/EUR, 0 for
                       JPY/KRW, 3 for KWD/OMR — the template picks the value
                       for its data source.
    """
    if not isinstance(value, dict):
        return value

    amount_field = args.get("amount_field", "amount")
    try:
        exponent = int(args.get("exponent", 2))
    except (TypeError, ValueError):
        return value
    if exponent < 0:
        return value

    amount = value.get(amount_field)
    if amount is None:
        return value

    try:
        amount_num = float(amount)
    except (TypeError, ValueError):
        return value

    # Detect already-decimal input vs minor-unit input. A string carrying a
    # decimal separator is taken at face value (no re-scaling); ints, floats,
    # and integer-shaped strings are treated as minor units to be divided.
    is_decimal_already = isinstance(amount, str) and ("." in amount or "," in amount)
    scaled = amount_num if is_decimal_already else amount_num / (10**exponent)

    if exponent == 0:
        value[amount_field] = f"{int(round(scaled)):,}"
    else:
        value[amount_field] = f"{scaled:,.{exponent}f}"
    return value


@register_transform("pick_fields")
def pick_fields(value: Any, args: Dict[str, Any]) -> Any:
    """Keep only the listed top-level keys in a dict; drop everything else.

    The first line of defence against tool-result bloat — most upstream MCP
    catalogue/lookup responses ship dozens of fields the agent never reads
    (full variant matrices, collection metadata, sku/barcode/weight, etc.).
    Path the rule at ``products[*]`` (or any single-item path) so the walker
    invokes this fn once per dict and we slim each one to its essentials.

    Args:
        keep — list of top-level keys to retain. Missing keys are silently
               omitted (a partial upstream payload never blows up a turn).
               Empty / missing keep list returns an empty dict.

    Non-dict inputs pass through unchanged so chaining stays safe.
    """
    if not isinstance(value, dict):
        return value
    keep = args.get("keep") or []
    return {k: value[k] for k in keep if k in value}


@register_transform("omit_fields")
def omit_fields(value: Any, args: Dict[str, Any]) -> Any:
    """Drop the listed top-level keys; keep everything else.

    Useful when most fields are wanted and only a few obvious offenders
    (protocol envelopes, debug metadata) need removing — saves enumerating
    every preserved key like ``pick_fields`` would. Use ``path: ""`` to
    drop top-level keys on the response root.

    Args:
        drop — list of top-level keys to remove. Missing keys are a no-op.

    Non-dict inputs pass through unchanged.
    """
    if not isinstance(value, dict):
        return value
    drop = set(args.get("drop") or ())
    return {k: v for k, v in value.items() if k not in drop}


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
# A stripped tag becomes a space, so ``word</b>.`` collapses to ``word .`` —
# drop the space a removed tag leaves immediately before closing punctuation.
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,;:!?)\]])")


@register_transform("strip_html")
def strip_html(value: Any, args: Dict[str, Any]) -> Any:
    """Strip HTML tags, collapse whitespace, optionally truncate at a word
    boundary.

    Upstream catalogue responses commonly include rich-text descriptions as
    HTML strings — the agent's prompt rules instruct it to render plain text
    anyway, so shipping the markup is pure waste. This op pulls the plain
    text out and (when ``max_chars`` is set) clips it at the nearest word
    boundary with an ellipsis so the LLM still sees a sentence-shaped
    excerpt rather than a jagged cut.

    Args:
        max_chars — optional positive int; when set, clip the result.
                    Word-boundary truncation; trailing ``…`` appended.

    Operates on string values. Non-string inputs pass through unchanged so
    upstream shape drift never crashes a turn.
    """
    if not isinstance(value, str):
        return value
    plain = _HTML_TAG_RE.sub(" ", value)
    plain = _WHITESPACE_RE.sub(" ", plain).strip()
    plain = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", plain)
    max_chars = args.get("max_chars")
    if isinstance(max_chars, int) and max_chars > 0 and len(plain) > max_chars:
        clipped = plain[:max_chars]
        # Cut at the last space so we don't end mid-word; fall back to a hard
        # cut if there's no space in the window (e.g. one giant token).
        space = clipped.rfind(" ")
        if space > 0:
            clipped = clipped[:space]
        plain = clipped + "…"
    return plain


@register_transform("derive_field")
def derive_field(value: Any, args: Dict[str, Any]) -> Any:
    """Derive a new field on a dict via regex-capture + format-template.

    Reads a string field, runs a regex (positional or named groups), and
    writes the formatted result to a destination field on the same dict.
    Purely a shape op — no domain knowledge — so templates can use it to
    build any derived string (URLs, IDs, display labels) from any existing
    string field on a response object.

    Args:
        from     — source field name (default: ``"id"``)
        pattern  — regex with capture groups. Named groups (``(?P<name>…)``)
                   substitute by ``{name}`` in ``template``; positional
                   groups substitute by ``{0}``, ``{1}``, …
        template — format string applied to the captured groups.
        to       — destination field name (required). Existing values at
                   ``to`` are overwritten.
        overwrite — bool, default ``True``. When ``False``, only write if
                    the destination is missing/None.

    Behavior:
        - Non-dict ``value``: pass-through.
        - Source missing / not a string / regex doesn't match: no-op.
        - Bad regex or template placeholder mismatch: logged warning, no-op.
    """
    if not isinstance(value, dict):
        return value

    src_field = args.get("from", "id")
    pattern = args.get("pattern")
    template = args.get("template")
    dst_field = args.get("to")
    overwrite = args.get("overwrite", True)

    if not pattern or not template or not dst_field:
        return value

    if not overwrite and value.get(dst_field) is not None:
        return value

    src = value.get(src_field)
    if not isinstance(src, str):
        return value

    try:
        match = re.search(pattern, src)
    except re.error as e:
        logger.warning(
            f"[derive_field] bad regex {pattern!r} for field {src_field!r}: {e}"
        )
        return value
    if not match:
        return value

    try:
        named = match.groupdict()
        if named and all(v is not None for v in named.values()):
            value[dst_field] = template.format(**named)
        else:
            value[dst_field] = template.format(*match.groups())
    except (IndexError, KeyError, ValueError) as e:
        # IndexError/KeyError: template references a capture group that
        # doesn't exist. ValueError: malformed format string (e.g. a bare
        # "{" or unbalanced braces). All are template-author bugs that
        # should no-op per this fn's documented contract, not abort the
        # whole transform pass for the response.
        logger.warning(
            f"[derive_field] template {template!r} does not fit captures of "
            f"{pattern!r}: {e}"
        )

    return value


__all__ = [
    "TRANSFORM_REGISTRY",
    "TransformFn",
    "apply_response_transforms",
    "derive_field",
    "omit_fields",
    "pick_fields",
    "register_transform",
    "scale_by_exponent",
    "strip_html",
]
