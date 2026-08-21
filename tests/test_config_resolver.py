"""Unit tests for the shared config-precedence resolver primitives.

Covers FieldSpec/resolve_field/resolve_fields in isolation: tier
precedence, lazy evaluation (later tiers must not be called once an
earlier one resolves), sync vs async tiers, and required-field errors.
"""

import pytest

from app.core.config.resolver import (
    FieldSpec,
    resolve_field,
    resolve_fields,
)


@pytest.mark.asyncio
async def test_first_non_none_tier_wins():
    spec = FieldSpec(name="x", tiers=[None, "second", "third"])
    assert await resolve_field(spec) == "second"


@pytest.mark.asyncio
async def test_plain_value_tier():
    spec = FieldSpec(name="x", tiers=["value"])
    assert await resolve_field(spec) == "value"


@pytest.mark.asyncio
async def test_sync_callable_tier():
    spec = FieldSpec(name="x", tiers=[lambda: None, lambda: "resolved"])
    assert await resolve_field(spec) == "resolved"


@pytest.mark.asyncio
async def test_async_callable_tier():
    async def getter():
        return "async-value"

    spec = FieldSpec(name="x", tiers=[None, getter])
    assert await resolve_field(spec) == "async-value"


@pytest.mark.asyncio
async def test_later_tiers_not_evaluated_once_resolved():
    calls = []

    def tier_a():
        calls.append("a")
        return "value-a"

    def tier_b():
        calls.append("b")
        return "value-b"

    spec = FieldSpec(name="x", tiers=[tier_a, tier_b])
    result = await resolve_field(spec)
    assert result == "value-a"
    assert calls == ["a"]


@pytest.mark.asyncio
async def test_all_none_returns_none_when_not_required():
    spec = FieldSpec(name="x", tiers=[None, lambda: None])
    assert await resolve_field(spec) is None


@pytest.mark.asyncio
async def test_required_field_missing_raises_with_custom_message():
    spec = FieldSpec(
        name="x",
        tiers=[None],
        required=True,
        error_message="x is required",
    )
    with pytest.raises(ValueError, match="x is required"):
        await resolve_field(spec)


@pytest.mark.asyncio
async def test_required_field_missing_raises_default_message():
    spec = FieldSpec(name="x", tiers=[None], required=True)
    with pytest.raises(ValueError, match="x has no value from any tier"):
        await resolve_field(spec)


@pytest.mark.asyncio
async def test_required_field_present_does_not_raise():
    spec = FieldSpec(name="x", tiers=[None, "ok"], required=True)
    assert await resolve_field(spec) == "ok"


@pytest.mark.asyncio
async def test_resolve_fields_returns_flat_dict():
    specs = [
        FieldSpec(name="a", tiers=["1"]),
        FieldSpec(name="b", tiers=[None, "2"]),
    ]
    assert await resolve_fields(specs) == {"a": "1", "b": "2"}


@pytest.mark.asyncio
async def test_zero_and_false_are_terminal_values_not_none():
    # 0 and False are valid resolved values; only None should fall through.
    spec_zero = FieldSpec(name="x", tiers=[0, "fallback"])
    spec_false = FieldSpec(name="y", tiers=[False, "fallback"])
    assert await resolve_field(spec_zero) == 0
    assert await resolve_field(spec_false) is False
