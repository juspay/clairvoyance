"""The standing rule handed to the SDK: the draw window Juspay checks."""

from decimal import Decimal

import pytest

from app.services.preview.uap.utils import build_transit_intent


def _intent(max_per_draw="200.00"):
    return build_transit_intent(
        ["Chennai Metro Rail Limited"],
        max_per_draw=max_per_draw,
        max_total="7500.00",
        max_draws=40,
        validity_days=90,
    )


def test_quoted_amount_and_tolerance_span_zero_to_the_per_draw_cap() -> None:
    rule = _intent("200.00")
    assert rule.quoted_amount == "100.00" and rule.tolerance == "100.00"
    lo = Decimal(rule.quoted_amount) - Decimal(rule.tolerance)
    hi = Decimal(rule.quoted_amount) + Decimal(rule.tolerance)
    assert lo == Decimal("0.00") and hi == Decimal(rule.max_per_draw)


def test_window_follows_an_odd_cap_to_the_paisa() -> None:
    rule = _intent("35.00")
    assert rule.quoted_amount == "17.50" and rule.tolerance == "17.50"


def test_both_fields_are_on_the_wire_as_strings() -> None:
    wire = _intent().model_dump()
    assert isinstance(wire["quoted_amount"], str)
    assert isinstance(wire["tolerance"], str)


def test_unbound_intent_is_refused() -> None:
    with pytest.raises(ValueError):
        build_transit_intent(
            [],
            max_per_draw="200.00",
            max_total="7500.00",
            max_draws=40,
            validity_days=90,
        )
