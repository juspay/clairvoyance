"""Tests for the governed knob registry (config resolver Layer 2).

Covers the three guarantees the registry exists to provide:
  1. One precedence order for every knob
     (playground > template > merchant > platform).
  2. Floors/ceilings are enforced — a merchant knob can never cross them.
  3. Unregistered config is fatal, not silently None.

No Redis runs in unit tests, so the platform tier resolves
env -> default via get_config (ENABLE_REDIS_DYNAMIC_CONFIG defaults False).
"""

import ast
import pathlib
import warnings
from types import SimpleNamespace

import pytest

import app.core.config.resolver as resolver_mod
from app.core.config.resolver import REGISTRY, Knob, Scope, register, resolve

# ---------------------------------------------------------------------------
#  Registry declarations are self-validating at import time
# ---------------------------------------------------------------------------


def test_registry_is_populated():
    assert REGISTRY, "registry should ship with knobs declared"


def test_every_registered_knob_is_internally_consistent():
    """Each declaration must satisfy its own invariants (Knob.__post_init__).

    Reconstructing each knob re-runs validation, so a bad edit to the
    REGISTRY block fails here rather than at some 3am call site.
    """
    for key, knob in REGISTRY.items():
        assert knob.key == key, f"{key!r} filed under a mismatched key"
        Knob(
            key=knob.key,
            type=knob.type,
            default=knob.default,
            description=knob.description,
            scope=knob.scope,
            merchant_field=knob.merchant_field,
            floor=knob.floor,
            ceiling=knob.ceiling,
        )


def test_merchant_scope_requires_a_merchant_field():
    with pytest.raises(ValueError, match="requires merchant_field"):
        Knob(
            key="X",
            type=int,
            default=1,
            description="d",
            scope=Scope.MERCHANT,
        )


def test_platform_scope_rejects_a_merchant_field():
    with pytest.raises(ValueError, match="meaningless for a PLATFORM"):
        Knob(
            key="X",
            type=int,
            default=1,
            description="d",
            scope=Scope.PLATFORM,
            merchant_field="foo",
        )


def test_bounds_rejected_on_non_numeric_knob():
    with pytest.raises(ValueError, match="only apply to numeric"):
        Knob(key="X", type=str, default="a", description="d", floor=1)


def test_inverted_bounds_rejected():
    with pytest.raises(ValueError, match=r"floor \(10\) > ceiling \(1\)"):
        Knob(key="X", type=int, default=5, description="d", floor=10, ceiling=1)


def test_default_outside_its_own_bounds_rejected():
    with pytest.raises(ValueError, match="outside"):
        Knob(key="X", type=int, default=99, description="d", floor=0, ceiling=10)


def test_duplicate_key_rejected():
    knob = Knob(key="BB_CALL_MAX_RETRY", type=int, default=1, description="d")
    with pytest.raises(ValueError, match="Duplicate knob key"):
        register(knob)


# ---------------------------------------------------------------------------
#  Precedence
# ---------------------------------------------------------------------------


async def test_falls_back_to_platform_default_with_no_overrides():
    assert await resolve("BB_CALL_MAX_RETRY") == 3


async def test_merchant_overrides_platform():
    merchant = SimpleNamespace(max_retry=7)
    assert await resolve("BB_CALL_MAX_RETRY", merchant_config=merchant) == 7


async def test_template_overrides_merchant():
    merchant = SimpleNamespace(max_retry=7)
    resolved = await resolve(
        "BB_CALL_MAX_RETRY", merchant_config=merchant, template_value=5
    )
    assert resolved == 5


async def test_playground_overrides_everything():
    merchant = SimpleNamespace(max_retry=7)
    resolved = await resolve(
        "BB_CALL_MAX_RETRY",
        merchant_config=merchant,
        template_value=5,
        playground_value=2,
    )
    assert resolved == 2


async def test_none_at_a_tier_falls_through_rather_than_winning():
    """An absent override must not shadow a lower tier with None."""
    merchant = SimpleNamespace(max_retry=7)
    resolved = await resolve(
        "BB_CALL_MAX_RETRY",
        merchant_config=merchant,
        template_value=None,
        playground_value=None,
    )
    assert resolved == 7


async def test_merchant_config_missing_the_field_falls_through():
    """A merchant row that simply doesn't set the knob is not an override."""
    merchant = SimpleNamespace()
    assert await resolve("BB_CALL_MAX_RETRY", merchant_config=merchant) == 3


async def test_merchant_config_none_falls_through():
    assert await resolve("BB_CALL_MAX_RETRY", merchant_config=None) == 3


# ---------------------------------------------------------------------------
#  Floors and ceilings — the core governance guarantee
# ---------------------------------------------------------------------------


async def test_merchant_cannot_exceed_ceiling():
    merchant = SimpleNamespace(max_retry=9999)
    assert await resolve("BB_CALL_MAX_RETRY", merchant_config=merchant) == 10


async def test_merchant_cannot_go_below_floor():
    """Floor stops a merchant dialling a number every few seconds."""
    merchant = SimpleNamespace(retry_offset=1)
    resolved = await resolve("BB_CALL_RETRY_OFFSET_MINUTES", merchant_config=merchant)
    assert resolved == 5


async def test_template_is_clamped_too():
    assert await resolve("BB_CALL_MAX_RETRY", template_value=500) == 10


async def test_playground_is_clamped_too():
    """Playground is highest precedence but still not above the platform."""
    assert await resolve("BB_CALL_MAX_RETRY", playground_value=-4) == 0


async def test_value_inside_bounds_is_untouched():
    merchant = SimpleNamespace(max_retry=4)
    assert await resolve("BB_CALL_MAX_RETRY", merchant_config=merchant) == 4


async def test_boundary_values_are_inclusive():
    for value, expected in ((0, 0), (10, 10)):
        merchant = SimpleNamespace(max_retry=value)
        assert await resolve("BB_CALL_MAX_RETRY", merchant_config=merchant) == expected


async def test_clamp_is_logged(monkeypatch):
    """A clamp is a misconfiguration — it must not happen silently.

    The app logs through loguru, which does not propagate to pytest's
    ``caplog``, so capture by swapping the module's logger (same approach as
    tests/breeze_buddy/test_template_wire_alias.py).
    """
    captured = []

    class _Stub:
        def warning(self, msg, *args, **kwargs):
            captured.append(str(msg).format(*args) if args else str(msg))

    monkeypatch.setattr(resolver_mod, "logger", _Stub())
    merchant = SimpleNamespace(max_retry=9999)
    await resolve("BB_CALL_MAX_RETRY", merchant_config=merchant)

    assert any("clamped" in line for line in captured), captured
    assert any("BB_CALL_MAX_RETRY" in line for line in captured)


async def test_no_clamp_no_warning(monkeypatch):
    """An in-range value must stay quiet — no warning fatigue."""
    captured = []

    class _Stub:
        def warning(self, msg, *args, **kwargs):
            captured.append(str(msg))

    monkeypatch.setattr(resolver_mod, "logger", _Stub())
    merchant = SimpleNamespace(max_retry=4)
    await resolve("BB_CALL_MAX_RETRY", merchant_config=merchant)

    assert captured == []


# ---------------------------------------------------------------------------
#  Type coercion — tenant tiers are raw, unlike the platform tier
# ---------------------------------------------------------------------------


async def test_numeric_string_override_is_coerced():
    """Template payloads are JSON; "5" and 5 both occur in the wild."""
    assert await resolve("BB_CALL_MAX_RETRY", template_value="5") == 5


async def test_coerced_string_override_is_still_clamped():
    assert await resolve("BB_CALL_MAX_RETRY", template_value="500") == 10


async def test_uncoercible_override_falls_through_with_a_warning(monkeypatch):
    """Garbage at one tier degrades like an absent value, loudly."""
    captured = []

    class _Stub:
        def warning(self, msg, *args, **kwargs):
            captured.append(str(msg))

    monkeypatch.setattr(resolver_mod, "logger", _Stub())
    merchant = SimpleNamespace(max_retry=7)
    resolved = await resolve(
        "BB_CALL_MAX_RETRY", merchant_config=merchant, template_value="banana"
    )

    assert resolved == 7
    assert any("template" in line and "banana" in line for line in captured), captured


async def test_unbounded_knob_passes_string_through():
    merchant = SimpleNamespace(inbound_call_timezone="Europe/Berlin")
    resolved = await resolve("BB_MERCHANT_DEFAULT_TIMEZONE", merchant_config=merchant)
    assert resolved == "Europe/Berlin"


# ---------------------------------------------------------------------------
#  Unregistered keys
# ---------------------------------------------------------------------------


async def test_unregistered_key_raises():
    with pytest.raises(KeyError, match="not a registered knob"):
        await resolve("BB_TOTALLY_MADE_UP_KNOB")


async def test_platform_scoped_knob_ignores_merchant_config(monkeypatch):
    """A PLATFORM knob must not be movable by a merchant row."""
    monkeypatch.setitem(
        REGISTRY,
        "BB_TEST_PLATFORM_ONLY",
        Knob(
            key="BB_TEST_PLATFORM_ONLY",
            type=int,
            default=42,
            description="test-only",
            scope=Scope.PLATFORM,
        ),
    )
    merchant = SimpleNamespace(BB_TEST_PLATFORM_ONLY=1, max_retry=1)
    assert await resolve("BB_TEST_PLATFORM_ONLY", merchant_config=merchant) == 42


# ---------------------------------------------------------------------------
#  Enforcement of the registry's own rules
#
#  These make the "no config sprawl" rules mechanical instead of aspirational —
#  the registry only helps if new env reads can't quietly appear beside it.
# ---------------------------------------------------------------------------

# Modules allowed to touch os.environ directly. Everything else must go
# through resolve() / get_config(). Keep this list SHORT and justified.
_ENV_READ_ALLOWLIST = {
    # The static tier itself — this *is* the env layer.
    "app/core/config/static.py",
    # get_config's environment fallback — the env tier implementation.
    "app/services/live_config/utils.py",
}


def _iter_app_py_files():
    app_root = pathlib.Path(__file__).resolve().parent.parent / "app"
    return sorted(app_root.rglob("*.py"))


def _reads_env(source: str) -> bool:
    """True if the module actually reads the environment.

    Parses rather than grepping: a docstring that *mentions* ``os.environ``
    (this rule is documented in the resolver itself) is not a violation.
    Detects ``os.environ...`` and ``os.getenv(...)``, plus the
    ``from os import environ/getenv`` spellings.
    """
    with warnings.catch_warnings():
        # Some modules carry unrelated invalid-escape DeprecationWarnings in
        # prompt strings; parsing them is not this test's concern.
        warnings.simplefilter("ignore", DeprecationWarning)
        tree = ast.parse(source)

    bare_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name in ("environ", "getenv"):
                    bare_names.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        # os.environ / os.getenv
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
            if isinstance(node.value, ast.Name) and node.value.id == "os":
                return True
        # environ[...] / getenv(...) imported directly
        if isinstance(node, ast.Name) and node.id in bare_names:
            return True
    return False


def test_module_code_never_reads_os_environ_directly():
    """Config must flow through the resolver, not ad-hoc os.environ reads.

    ~196 loose env constants in static.py are the scar this rule prevents.
    If this fails: register a Knob and call resolve(), or justify an
    allowlist entry.
    """
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []

    for path in _iter_app_py_files():
        rel = path.relative_to(repo_root).as_posix()
        if rel in _ENV_READ_ALLOWLIST:
            continue
        if _reads_env(path.read_text(encoding="utf-8")):
            offenders.append(rel)

    assert not offenders, (
        "These modules read the environment directly instead of going "
        f"through the config resolver: {offenders}"
    )


def test_env_read_allowlist_entries_still_exist():
    """A stale allowlist silently widens the rule — keep it honest."""
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    missing = [rel for rel in _ENV_READ_ALLOWLIST if not (repo_root / rel).is_file()]
    assert not missing, f"allowlist references files that no longer exist: {missing}"
