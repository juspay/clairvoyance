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


__all__ = [
    "TRANSFORM_REGISTRY",
    "TransformFn",
    "apply_response_transforms",
    "register_transform",
    "scale_by_exponent",
]
