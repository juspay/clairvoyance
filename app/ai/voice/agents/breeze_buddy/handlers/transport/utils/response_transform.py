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


@register_transform("format_number")
def format_number(value: Any, args: Dict[str, Any]) -> Any:
    """Derive a display string from a numeric field on the same dict.

    The label-derivation workhorse behind "display values are
    pre-formatted — never build your own": templates derive
    ``duration_label`` ("51 min"), ``distance_label`` ("13.1 km") and fare
    labels ("₹30") server-side so the LLM (and data-bound UI) only ever
    shows ready-made strings.

    Args:
        field     — source numeric field name (required).
        to        — destination field name (required). Overwritten.
        divide_by — divide the source value first (e.g. 60 s→min, 1000 m→km).
        decimals  — decimal places for the result (default 0 → rounded int).
        skip_zero — when true, a zero source value derives nothing (walk
                    legs must not grow a "₹0" fare label).
        prefix    — literal prefix (e.g. "₹").
        suffix    — literal suffix (e.g. " min").

    Non-dict values, missing/non-numeric sources: pass-through no-op.
    """
    if not isinstance(value, dict):
        return value
    src_field = args.get("field")
    dst_field = args.get("to")
    if not src_field or not dst_field:
        return value
    raw = value.get(src_field)
    try:
        number = float(raw)  # pyrefly: ignore[bad-argument-type]
    except (TypeError, ValueError):
        return value
    if args.get("skip_zero") and number == 0:
        return value
    divide_by = args.get("divide_by")
    if isinstance(divide_by, (int, float)) and divide_by:
        number = number / divide_by
    decimals = args.get("decimals", 0)
    if not isinstance(decimals, int) or decimals < 0:
        decimals = 0
    if decimals == 0:
        body = str(int(round(number)))
    else:
        body = f"{number:.{decimals}f}"
    prefix = args.get("prefix") or ""
    suffix = args.get("suffix") or ""
    value[dst_field] = f"{prefix}{body}{suffix}"
    return value


@register_transform("coalesce")
def coalesce(value: Any, args: Dict[str, Any]) -> Any:
    """Write the first non-empty of several (dotted) source fields to a
    destination field on the same dict — the display-layer if/else that
    the declarative render grammar deliberately lacks (e.g. a transit
    leg's title is its route code when it has one, else its mode; its
    value line is the fare when priced, else the walking distance).

    Args:
        fields  — ordered list of source paths on this dict; a path may be
                  dotted to reach nested dicts ("route.code").
        to      — destination field name (required). Overwritten.
        default — literal written when NO field resolves (optional). Lets
                  a downstream hard bind (top-level component prop) rely
                  on the field existing — e.g. selected_tier_type "" on
                  modes whose API omits a selected tier.

    Non-dict values / no field resolving to a non-empty scalar (and no
    default): no-op.
    """
    if not isinstance(value, dict):
        return value
    fields = args.get("fields")
    dst_field = args.get("to")
    if not isinstance(fields, list) or not dst_field:
        return value
    if "default" in args:
        value.setdefault(dst_field, args["default"])
        # A present-but-None value still needs replacing — None fails the
        # bind resolver's `is None` check, which is what `default` exists
        # to prevent.
        if value[dst_field] is None:
            value[dst_field] = args["default"]
    for path in fields:
        if not isinstance(path, str) or not path:
            continue
        current: Any = value
        for key in path.split("."):
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current is not None and not isinstance(current, (dict, list)):
            text = str(current)
            if text.strip():
                value[dst_field] = current
                return value
    return value


@register_transform("format_time")
def format_time(value: Any, args: Dict[str, Any]) -> Any:
    """Derive a short clock label ("11:18") from an ISO-8601 field on the
    same dict — the display counterpart of format_number for timestamps
    (departure/arrival chips and the like).

    Args:
        field     — source ISO-8601 string field name (required).
        to        — destination field name (required). Overwritten.
        tz_offset — minutes to shift a UTC/naive timestamp into the
                    display zone (e.g. 330 for IST). Timestamps that
                    already carry an offset are converted, not shifted twice.
        fmt       — strftime format (default "%H:%M").

    Non-dict values, missing/unparseable sources: pass-through no-op.
    """
    if not isinstance(value, dict):
        return value
    src_field = args.get("field")
    dst_field = args.get("to")
    if not src_field or not dst_field:
        return value
    raw = value.get(src_field)
    if not isinstance(raw, str) or not raw:
        return value
    from datetime import datetime, timedelta, timezone

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    tz_offset = args.get("tz_offset")
    if isinstance(tz_offset, (int, float)) and tz_offset:
        parsed = parsed.astimezone(timezone(timedelta(minutes=tz_offset)))
    fmt = args.get("fmt")
    if not isinstance(fmt, str) or not fmt:
        fmt = "%H:%M"
    try:
        value[dst_field] = parsed.strftime(fmt)
    except ValueError:
        return value
    return value


@register_transform("join_list")
def join_list(value: Any, args: Dict[str, Any]) -> Any:
    """Join a list-of-strings field into one display string on the same dict.

    E.g. ``modes: ["Walk", "Metro", "Walk"]`` →
    ``modes_label: "Walk → Metro → Walk"``.

    Args:
        field     — source list field name (required).
        to        — destination field name (required). Overwritten.
        separator — joiner (default " → ").

    Non-dict values, missing/non-list sources: pass-through no-op.
    Non-string entries are stringified.
    """
    if not isinstance(value, dict):
        return value
    src_field = args.get("field")
    dst_field = args.get("to")
    if not src_field or not dst_field:
        return value
    raw = value.get(src_field)
    if not isinstance(raw, list):
        return value
    separator = args.get("separator")
    if not isinstance(separator, str):
        separator = " → "
    value[dst_field] = separator.join(str(item) for item in raw)
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
# A stripped tag becomes a space, so ``word</b>.`` would collapse to ``word .``.
# Drop the run of tags (and the whitespace between/after them) that sits
# immediately before closing punctuation, keeping only the punctuation — this
# also handles nested closes like ``<em>x</em></strong>.`` -> ``x.``. Anchored
# to the tag site on purpose: a global "space before punctuation" pass also
# corrupts tag-free text — decimals ("3 . 14" -> "3. 14"), spaced parens
# ("( a )" -> "( a)"), initials ("Mr . Smith").
_TAG_BEFORE_PUNCT_RE = re.compile(r"(?:<[^>]+>\s*)+([.,;:!?)\]])")


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
    # Drop tags sitting directly before closing punctuation first (no injected
    # space), then turn the remaining tags into spaces and collapse runs.
    plain = _TAG_BEFORE_PUNCT_RE.sub(r"\1", value)
    plain = _HTML_TAG_RE.sub(" ", plain)
    plain = _WHITESPACE_RE.sub(" ", plain).strip()
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


@register_transform("map_values")
def map_values(value: Any, args: Dict[str, Any]) -> Any:
    """Map a field's value(s) through a lookup table onto a destination
    field on the same dict — the display-layer rename the render grammar
    deliberately lacks (e.g. a transit wire mode "Subway" reads as
    "Train" to commuters).

    Args:
        field   — source field name (required). Scalar or list of scalars.
        to      — destination field name (required). Overwritten.
        map     — {source value (as string) → replacement} (required).
        default — value for unmapped entries; omitted = keep the original.

    A LIST source maps each element (writing a new list). Non-dict
    values, missing source, or a non-dict ``map``: pass-through no-op.
    """
    if not isinstance(value, dict):
        return value
    src_field = args.get("field")
    dst_field = args.get("to")
    table = args.get("map")
    if not src_field or not dst_field or not isinstance(table, dict):
        return value
    raw = value.get(src_field)
    if raw is None:
        return value
    mapping: Dict[str, Any] = table

    def _one(item: Any) -> Any:
        key = str(item)
        if key in mapping:
            return mapping[key]
        return args.get("default", item)

    if isinstance(raw, list):
        value[dst_field] = [_one(item) for item in raw]
    else:
        value[dst_field] = _one(raw)
    return value


@register_transform("format_range")
def format_range(value: Any, args: Dict[str, Any]) -> Any:
    """Derive a display label from a min/max pair on the same dict:
    "₹30" when they agree, "₹10–55" when they span (a fare across
    service classes). The two-field counterpart of format_number.

    Args:
        min_field — source field for the lower bound (required).
        max_field — source field for the upper bound (required).
        to        — destination field name (required). Overwritten.
        prefix    — prepended once (default "").
        suffix    — appended once (default "").
        decimals  — round to N decimals; default 0 drops any ".0".
        skip_zero — True: both bounds zero → no label written.
        style     — "span" (default): "₹10–55"; "from": "from ₹10" — the
                    consumer-friendly form when the spread is a class
                    ladder, not price uncertainty. Equal bounds always
                    render plain ("₹30") in either style.

    Non-dict values or non-numeric bounds: pass-through no-op. A missing
    max falls back to min (and vice versa) so a single-priced row still
    labels.
    """
    if not isinstance(value, dict):
        return value
    min_field = args.get("min_field")
    max_field = args.get("max_field")
    dst_field = args.get("to")
    if not min_field or not max_field or not dst_field:
        return value
    lo_raw = value.get(min_field)
    hi_raw = value.get(max_field)
    if lo_raw is None and hi_raw is None:
        return value
    try:
        lo = float(lo_raw if lo_raw is not None else hi_raw)  # type: ignore[arg-type]
        hi = float(hi_raw if hi_raw is not None else lo_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return value
    if args.get("skip_zero") and lo == 0 and hi == 0:
        return value
    decimals = args.get("decimals", 0)
    if not isinstance(decimals, int) or decimals < 0:
        decimals = 0

    def _fmt(number: float) -> str:
        text = f"{number:.{decimals}f}"
        if decimals == 0 and text.endswith(".0"):
            text = text[:-2]
        return text

    prefix = args.get("prefix") or ""
    suffix = args.get("suffix") or ""
    if lo == hi:
        label = f"{prefix}{_fmt(lo)}{suffix}"
    elif args.get("style") == "from":
        label = f"from {prefix}{_fmt(min(lo, hi))}{suffix}"
    else:
        label = f"{prefix}{_fmt(min(lo, hi))}–{_fmt(max(lo, hi))}{suffix}"
    value[dst_field] = label
    return value


@register_transform("mark_extremum")
def mark_extremum(value: Any, args: Dict[str, Any]) -> Any:
    """Tag the min/max element of a list field — the cross-item ranking a
    per-item transform cannot express (e.g. label the fastest / cheapest
    journey of a result set). Apply at the path of the dict HOLDING the
    list (root for a top-level list).

    Args:
        list_field  — field on this dict holding the list (required).
        field       — numeric field ranked on each element (required).
        mode        — "min" (default) or "max".
        to          — destination field on the WINNING element (required).
        label       — value written to `to` (required).
        skip_if_set — True (default): elements whose `to` is already set
                      are not overwritten AND are skipped when ranking, so
                      a second rule ("Cheapest" after "Fastest") tags the
                      best of the REMAINING elements.

    Elements without a numeric `field` are ignored. No qualifying
    element / bad args: no-op.
    """
    if not isinstance(value, dict):
        return value
    list_field = args.get("list_field")
    field = args.get("field")
    dst_field = args.get("to")
    label = args.get("label")
    if not list_field or not field or not dst_field or label is None:
        return value
    items = value.get(list_field)
    if not isinstance(items, list):
        return value
    skip_if_set = args.get("skip_if_set", True)
    best: Optional[Dict[str, Any]] = None
    best_num: Optional[float] = None
    use_max = args.get("mode") == "max"
    for item in items:
        if not isinstance(item, dict):
            continue
        if skip_if_set and item.get(dst_field) is not None:
            continue
        raw = item.get(field)
        try:
            num = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if best_num is None or (num > best_num if use_max else num < best_num):
            best_num = num
            best = item
    if best is not None:
        best[dst_field] = label
    return value


@register_transform("count_where")
def count_where(value: Any, args: Dict[str, Any]) -> Any:
    """Count a list field's elements by a member-field predicate and write
    the (offset, clamped) count — e.g. transfers = transit legs minus one.

    Args:
        list_field — field on this dict holding the list (required).
        field      — member field inspected on each element (required).
        in         — count only elements whose field (as string) is in
                     this list.
        not_in     — count only elements whose field is NOT in this list.
        offset     — added to the count (default 0), e.g. -1 for
                     transfers.
        min        — lower clamp after offset (default 0).
        to         — destination field on this dict (required).

    Missing/non-list source or bad args: no-op.
    """
    if not isinstance(value, dict):
        return value
    list_field = args.get("list_field")
    field = args.get("field")
    dst_field = args.get("to")
    if not list_field or not field or not dst_field:
        return value
    items = value.get(list_field)
    if not isinstance(items, list):
        return value
    include = args.get("in")
    exclude = args.get("not_in")
    count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        member = str(item.get(field))
        if isinstance(include, list) and member not in [str(x) for x in include]:
            continue
        if isinstance(exclude, list) and member in [str(x) for x in exclude]:
            continue
        count += 1
    offset = args.get("offset", 0)
    floor = args.get("min", 0)
    if not isinstance(offset, int):
        offset = 0
    if not isinstance(floor, int):
        floor = 0
    value[dst_field] = max(floor, count + offset)
    return value


@register_transform("find_where")
def find_where(value: Any, args: Dict[str, Any]) -> Any:
    """Find the FIRST element of a list field matching a member-field
    predicate and lift chosen member fields onto this dict — e.g. surface
    the transit leg's order as a journey-level field a card action can
    pass into an intent payload.

    Args:
        list_field — field on this dict holding the list (required).
        field      — member field inspected on each element (required).
        in         — match only elements whose field (as string) is in
                     this list.
        not_in     — match only elements whose field is NOT in this list.
        set        — {destination field on this dict: source field on the
                     matched element} (required, non-empty).

    Source fields missing on the match copy as absent (not None-stomped).
    No match / missing list / bad args: no-op.
    """
    if not isinstance(value, dict):
        return value
    list_field = args.get("list_field")
    field = args.get("field")
    mapping = args.get("set")
    if not list_field or not field or not isinstance(mapping, dict) or not mapping:
        return value
    items = value.get(list_field)
    if not isinstance(items, list):
        return value
    include = args.get("in")
    exclude = args.get("not_in")
    for item in items:
        if not isinstance(item, dict):
            continue
        member = str(item.get(field))
        if isinstance(include, list) and member not in [str(x) for x in include]:
            continue
        if isinstance(exclude, list) and member in [str(x) for x in exclude]:
            continue
        for dst, src in mapping.items():
            if not isinstance(dst, str) or not isinstance(src, str):
                continue
            if src in item:
                value[dst] = item[src]
        break
    return value


@register_transform("upcoming_times")
def upcoming_times(value: Any, args: Dict[str, Any]) -> Any:
    """Reduce a service-day timetable to the next departures after "now" —
    e.g. a transit feed's ``nextAvailableTimings`` (a full-day list of
    "HH:MM:SS" strings or ``[arrival, departure]`` pairs) into
    "5:23 PM, 5:53 PM". Times are local clock strings in the zone given by
    ``tz_offset``; "now" is computed in that zone at transform time.

    Args:
        field     — source list field on this dict (required). Elements:
                    "HH:MM[:SS]" strings, or lists/tuples of them (the
                    LAST element of a pair is used — the departure).
        to        — destination field (required). Written ONLY when at
                    least one upcoming time exists — bind-guards and `if`
                    gates can rely on absence meaning "no more today".
        count     — how many to keep (default 2).
        tz_offset — minutes from UTC for "now" (default 0; IST = 330).
        separator — join string (default ", ").
        fmt       — strftime for the label (default "%-I:%M %p" — "5:23 PM").

    Non-dict values / missing list / bad args: no-op.
    """
    if not isinstance(value, dict):
        return value
    src_field = args.get("field")
    dst_field = args.get("to")
    if not src_field or not dst_field:
        return value
    raw = value.get(src_field)
    if not isinstance(raw, list):
        return value
    from datetime import datetime, timedelta, timezone

    count = args.get("count", 2)
    if not isinstance(count, int) or count < 1:
        count = 2
    offset = args.get("tz_offset", 0)
    if not isinstance(offset, (int, float)):
        offset = 0
    fmt = args.get("fmt", "%-I:%M %p")
    separator = args.get("separator", ", ")
    now_local = datetime.now(timezone.utc) + timedelta(minutes=offset)
    now_secs = now_local.hour * 3600 + now_local.minute * 60 + now_local.second

    labels: List[str] = []
    for entry in raw:
        if isinstance(entry, (list, tuple)) and entry:
            entry = entry[-1]  # [arrival, departure] → departure
        if not isinstance(entry, str):
            continue
        parts = entry.split(":")
        try:
            h, m = int(parts[0]), int(parts[1])
            s = int(parts[2]) if len(parts) > 2 else 0
        except (ValueError, IndexError):
            continue
        if h * 3600 + m * 60 + s <= now_secs:
            continue
        try:
            label = now_local.replace(hour=h % 24, minute=m, second=0).strftime(fmt)
        except ValueError:
            continue
        labels.append(label)
        if len(labels) >= count:
            break
    if labels:
        value[dst_field] = separator.join(labels)
    return value


__all__ = [
    "TRANSFORM_REGISTRY",
    "TransformFn",
    "apply_response_transforms",
    "derive_field",
    "count_where",
    "find_where",
    "upcoming_times",
    "format_range",
    "map_values",
    "mark_extremum",
    "omit_fields",
    "pick_fields",
    "register_transform",
    "scale_by_exponent",
    "strip_html",
]
