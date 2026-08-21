"""The one place runtime configuration precedence is decided.

Two layers live here.

**Layer 1 — the ``FieldSpec`` primitive** (unregistered, ad-hoc chains).
Unifies the "template > per-provider override > dynamic (Redis) > static
(env)" precedence chains scattered across VAD, TTS, STT, and LLM config
resolution into one small primitive.

**Layer 2 — the governed knob registry** (``Knob`` / ``REGISTRY`` /
``resolve``). Every *tunable* — frequency caps, quiet-hour windows, merchant
default timezone — is declared once in ``REGISTRY`` and read through a single
``resolve()`` call with a single precedence order. This is what stops the
config sprawl that produced ~196 loose env constants in ``static.py``: a knob
that isn't in the registry has no defined precedence, no type, no bounds, and
no per-merchant story. See ``Knob`` for the tier order and the floor/ceiling
guarantee.

Layer 2 is built on Layer 1 — a registered knob is just a ``FieldSpec`` whose
tiers are derived from its ``Knob`` declaration, plus clamping.

A ``FieldSpec`` declares, for one output field, an ordered list of tiers to
try. Each tier is a plain value, a zero-arg sync callable, or a zero-arg
async callable (dynamic/Redis lookups). The first tier that yields a
non-None value wins. Tiers are evaluated lazily, in order — a later tier is
only invoked (and only awaited) if every earlier tier resolved to None. This
matters: several existing call sites only reach Redis on a template miss,
and eagerly evaluating every tier would add extra round-trips that don't
happen today.

A field may instead be ``required``: no matter how many tiers it has, if
none resolve to a non-None value, resolution raises. This covers both
"template-required, no fallback exists" (e.g. Vertex's ``model``) and
"dynamic-only-required" (e.g. Vertex's ``credentials_json``) uniformly —
only the tier list contents differ.

Deliberately out of scope for this primitive (kept as bespoke code at call
sites instead of being forced in):
  - Whole-object swaps with no cross-field precedence, where every field is
    self-sufficient at one tier (e.g. Deepgram's ``config.deepgram or
    DeepgramSTTConfig()``, SmartTurn's ``stt_config.smart_turn or
    SmartTurnConfig()``). There's no fallback chain to declare per field.
  - Conditionally-required fields, where requiredness itself depends on
    another field's value (e.g. Vertex's ``thinking.budget_tokens`` is only
    required when ``thinking.enabled``). ``FieldSpec.required`` is a static
    flag, not a predicate — expressing this in the resolver would be scope
    creep for a single caller.
  - Keyed/indirect lookups where the *name* of the dynamic config key is
    itself supplied by an upstream tier (Azure/OpenAI's ``api_key_name`` ->
    ``get_config(api_key_name, ...)``). This isn't a fallback chain either.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, TypeVar, Union

from app.core.logger import logger
from app.services.live_config.store import get_config

Tier = Union[Any, Callable[[], Any], Callable[[], "Awaitable[Any]"]]

T = TypeVar("T")


@dataclass
class FieldSpec:
    """One output field's precedence chain.

    ``tiers`` is walked in order; the first non-None result wins. If every
    tier yields None:
      - ``required=False`` (default): the field resolves to None.
      - ``required=True``: resolution raises ``ValueError(error_message)``.
    """

    name: str
    tiers: list[Tier] = field(default_factory=list)
    required: bool = False
    error_message: Optional[str] = None


def or_none(value: Optional[T]) -> Optional[T]:
    """Falsy -> None, so a truthy-checked field can feed a ``FieldSpec`` tier.

    ``resolve_field`` treats only None as "unset". Several pre-existing call
    sites instead fell back on a truthy check (``if cfg and cfg.field``), where
    an empty string or a 0 must still fall through to the next tier. Wrapping
    such a tier in ``or_none`` preserves that exact behavior. Use it only where
    the original code was truthy-checked — fields where 0/"" are legitimate
    values (e.g. ``temperature=0.0``) must stay a bare ``is not None`` tier.
    """
    return value if value else None


async def _resolve_tier(tier: Tier) -> Any:
    if callable(tier):
        result = tier()
        if inspect.isawaitable(result):
            result = await result
        return result
    return tier


async def resolve_field(spec: FieldSpec) -> Any:
    """Resolve a single field by walking its tiers, first non-None wins."""
    for tier in spec.tiers:
        val = await _resolve_tier(tier)
        if val is not None:
            return val
    if spec.required:
        raise ValueError(
            spec.error_message or f"{spec.name} has no value from any tier"
        )
    return None


async def resolve_fields(specs: list[FieldSpec]) -> dict[str, Any]:
    """Resolve a group of fields into a flat dict (for ``**kwargs`` into a Pydantic model)."""
    return {spec.name: await resolve_field(spec) for spec in specs}


# ===========================================================================
#  Layer 2 — the governed knob registry
# ===========================================================================


class Scope(str, Enum):
    """Who is allowed to move a knob."""

    PLATFORM = "platform"
    """Only we can change it (env / Redis). A merchant cannot override."""

    MERCHANT = "merchant"
    """A merchant may override it, but only within the knob's floor/ceiling."""


@dataclass(frozen=True)
class Knob:
    """One registered, governed tunable.

    Resolution order (lowest precedence first) — this order is fixed for
    *every* knob, which is the whole point of the registry::

        default -> env -> Redis/DevCycle -> merchant -> template -> playground
        \\_____________  ______________/   \\_________  ____________________/
                      \\/                             \\/
                 platform tiers                   tenant tiers
              (delegated to get_config)      (only if scope is MERCHANT)

    The first tier that yields a non-None value wins, walking from the
    highest precedence down. The result is then clamped into
    ``[floor, ceiling]``.

    **The floor/ceiling guarantee**: a resolved value is *always* within
    bounds, whatever tier produced it. Clamping is applied unconditionally
    rather than only to tenant tiers — an out-of-range value is a mistake
    wherever it came from (a fat-fingered Redis flag is no safer than a bad
    merchant setting), and an unconditional invariant is far easier to reason
    about and test than a source-dependent one. Every clamp is logged with
    the tier that caused it, so the mistake stays visible.

    Attributes:
        key: Registry key, and the env var / Redis flag name for the platform
            tiers. Uppercase snake_case by convention.
        type: Type the value is coerced to (``str``, ``int``, ``float``,
            ``bool``). Passed through to ``get_config``.
        default: Last-resort value. Must itself satisfy floor/ceiling.
        scope: PLATFORM or MERCHANT — see ``Scope``.
        merchant_field: Attribute name to read off the merchant config object
            passed to ``resolve``. Required when scope is MERCHANT.
        floor: Inclusive lower bound. Numeric knobs only.
        ceiling: Inclusive upper bound. Numeric knobs only.
        description: Why this knob exists / what turning it does.
    """

    key: str
    type: type
    default: Any
    description: str
    scope: Scope = Scope.PLATFORM
    merchant_field: Optional[str] = None
    floor: Optional[float] = None
    ceiling: Optional[float] = None

    def __post_init__(self) -> None:
        """Validate the declaration itself, at import time.

        A malformed knob is a programming error that should fail on the very
        first import rather than at 3am on the one call path that reads it.
        """
        if self.scope is Scope.MERCHANT and not self.merchant_field:
            raise ValueError(
                f"Knob {self.key!r}: scope=MERCHANT requires merchant_field "
                "(the attribute to read off the merchant config object)"
            )
        if self.scope is Scope.PLATFORM and self.merchant_field:
            raise ValueError(
                f"Knob {self.key!r}: merchant_field is meaningless for a "
                "PLATFORM-scoped knob — did you mean scope=Scope.MERCHANT?"
            )
        has_bounds = self.floor is not None or self.ceiling is not None
        if has_bounds and self.type not in (int, float):
            raise ValueError(
                f"Knob {self.key!r}: floor/ceiling only apply to numeric "
                f"knobs, got type={self.type.__name__}"
            )
        if (
            self.floor is not None
            and self.ceiling is not None
            and self.floor > self.ceiling
        ):
            raise ValueError(
                f"Knob {self.key!r}: floor ({self.floor}) > ceiling "
                f"({self.ceiling})"
            )
        if self.clamp(self.default) != self.default:
            raise ValueError(
                f"Knob {self.key!r}: default ({self.default!r}) is outside "
                f"its own floor/ceiling [{self.floor}, {self.ceiling}]"
            )

    def coerce(self, value: Any) -> Any:
        """Coerce ``value`` to the knob's declared type.

        The platform tier is coerced by ``get_config``; the tenant tiers are
        not, and their values routinely arrive as strings (template payloads
        are JSON, where ``"5"`` and ``5`` both occur). Without this, a
        string-typed override reaches ``clamp`` and raises TypeError.

        Raises:
            ValueError: if the value cannot be represented as the knob's type.
                Callers treat that like an absent value and fall through.
        """
        if self.type is bool:
            # bool("false") is True, so strings need explicit handling.
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in ("true", "1", "yes"):
                    return True
                if lowered in ("false", "0", "no"):
                    return False
                raise ValueError(f"{value!r} is not a boolean")
            return bool(value)
        try:
            return self.type(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{value!r} is not a valid {self.type.__name__}: {exc}"
            ) from None

    def clamp(self, value: Any) -> Any:
        """Pull ``value`` inside [floor, ceiling]. Non-numerics pass through."""
        if value is None or self.type not in (int, float):
            return value
        if self.floor is not None and value < self.floor:
            return self.type(self.floor)
        if self.ceiling is not None and value > self.ceiling:
            return self.type(self.ceiling)
        return value


# ---------------------------------------------------------------------------
#  THE REGISTRY
#
#  Every governed knob is declared here and nowhere else.
#
#  Adding a tunable? Add it here first. Do not add a bare ``os.environ.get``
#  to static.py — that is exactly the sprawl this registry exists to stop.
#  Module code must never read os.environ directly; go through ``resolve``.
#
#  MERCHANT-scoped knobs read from a ``CallExecutionConfig`` row, which is
#  already keyed by (reseller_id, merchant_id, template) and already carries
#  these columns — so per-merchant tuning needs no new table and no migration.
# ---------------------------------------------------------------------------

REGISTRY: dict[str, Knob] = {}


def register(knob: Knob) -> Knob:
    """Add a knob to the registry, rejecting duplicate keys."""
    if knob.key in REGISTRY:
        raise ValueError(f"Duplicate knob key {knob.key!r} in REGISTRY")
    REGISTRY[knob.key] = knob
    return knob


# --- Outbound calling: retry cadence -------------------------------------

register(
    Knob(
        key="BB_CALL_MAX_RETRY",
        type=int,
        default=3,
        floor=0,
        ceiling=10,
        scope=Scope.MERCHANT,
        merchant_field="max_retry",
        description=(
            "Retries per lead before it is abandoned. Ceiling exists so a "
            "merchant cannot turn a failed number into a dialling loop."
        ),
    )
)

register(
    Knob(
        key="BB_CALL_RETRY_OFFSET_MINUTES",
        type=int,
        default=60,
        floor=5,
        ceiling=1440,
        scope=Scope.MERCHANT,
        merchant_field="retry_offset",
        description=(
            "Gap between retry attempts. Floor keeps a merchant from "
            "hammering a number every few seconds."
        ),
    )
)

register(
    Knob(
        key="BB_CALL_INITIAL_OFFSET_MINUTES",
        type=int,
        default=0,
        floor=0,
        ceiling=1440,
        scope=Scope.MERCHANT,
        merchant_field="initial_offset",
        description="Delay between lead ingest and the first call attempt.",
    )
)

# --- Outbound calling: frequency caps ------------------------------------

register(
    Knob(
        key="BB_CALL_RATE_LIMIT_MAX_CALLS",
        type=int,
        default=100,
        floor=1,
        ceiling=10_000,
        scope=Scope.MERCHANT,
        merchant_field="rate_limit_max_calls",
        description=(
            "Frequency cap: calls allowed inside the rate-limit window. "
            "Ceiling is the platform's own throughput guard."
        ),
    )
)

register(
    Knob(
        key="BB_CALL_RATE_LIMIT_WINDOW_SECONDS",
        type=int,
        default=3600,
        floor=60,
        ceiling=86_400,
        scope=Scope.MERCHANT,
        merchant_field="rate_limit_window_seconds",
        description="Frequency-cap window that BB_CALL_RATE_LIMIT_MAX_CALLS applies over.",
    )
)

# --- Locale ---------------------------------------------------------------

register(
    Knob(
        key="BB_MERCHANT_DEFAULT_TIMEZONE",
        type=str,
        default="Asia/Kolkata",
        scope=Scope.MERCHANT,
        merchant_field="inbound_call_timezone",
        description=(
            "Timezone that quiet-hour windows are interpreted in. Not "
            "bounded — validity is a tz-database question, not a range."
        ),
    )
)


# ---------------------------------------------------------------------------
#  Resolution
# ---------------------------------------------------------------------------


async def resolve(
    key: str,
    *,
    merchant_config: Any = None,
    template_value: Any = None,
    playground_value: Any = None,
) -> Any:
    """Resolve one registered knob, for a merchant, with bounds enforced.

    This is the single entry point Layer 2 exists to provide: given a knob
    key and whatever context the caller happens to have, return the value to
    actually use.

    Args:
        key: A key present in ``REGISTRY``.
        merchant_config: The merchant's ``CallExecutionConfig`` (or any object
            exposing the knob's ``merchant_field``). Duck-typed on purpose —
            ``app.core`` must not import ``app.schemas``, and keeping it
            structural means tests can pass a stand-in. Ignored for
            PLATFORM-scoped knobs.
        template_value: Template-level override, or None.
        playground_value: Playground override (highest precedence), or None.

    Returns:
        The resolved value, coerced to the knob's type and clamped into
        ``[floor, ceiling]``.

    Raises:
        KeyError: if ``key`` is not registered. Unregistered config has no
            defined precedence, so this is deliberately fatal rather than a
            silent None.
    """
    try:
        knob = REGISTRY[key]
    except KeyError:
        raise KeyError(
            f"{key!r} is not a registered knob. Declare it in "
            "app/core/config/resolver.py REGISTRY — config with no "
            "declaration has no defined precedence, type, or bounds."
        ) from None

    # merchant_field is guaranteed non-None for MERCHANT scope by
    # Knob.__post_init__; bind it locally so that is visible to the checker.
    merchant_value = None
    merchant_field = knob.merchant_field
    if (
        knob.scope is Scope.MERCHANT
        and merchant_field is not None
        and merchant_config is not None
    ):
        merchant_value = getattr(merchant_config, merchant_field, None)

    # Highest precedence first; get_config covers Redis -> env -> default.
    tiers: list[tuple[str, Tier]] = [
        ("playground", playground_value),
        ("template", template_value),
        ("merchant", merchant_value),
        ("platform", lambda: get_config(knob.key, knob.default, knob.type)),
    ]

    for tier_name, tier in tiers:
        raw = await _resolve_tier(tier)
        if raw is None:
            continue
        try:
            value = knob.coerce(raw)
        except ValueError as exc:
            # A malformed override degrades like an absent one rather than
            # taking resolution down — but it is never silent.
            logger.warning(
                f"Knob {knob.key}: ignoring {tier_name} value {raw!r} ({exc}); "
                "falling through to the next tier"
            )
            continue
        clamped = knob.clamp(value)
        if clamped != value:
            logger.warning(
                f"Knob {knob.key}: {tier_name} value {value!r} outside "
                f"[{knob.floor}, {knob.ceiling}], clamped to {clamped!r}"
            )
        return clamped

    # get_config always yields at least knob.default, so this is unreachable
    # unless a tier explicitly resolved to None.
    return knob.default
