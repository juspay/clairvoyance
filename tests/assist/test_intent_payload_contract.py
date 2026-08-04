"""Widget ↔ backend ui_intent payload contract (RFC-001 §3.3).

The fixtures in ``fixtures/intent_payloads.json`` are the EXACT payload
JSON bodies the widget's commerce components build (one list per intent,
covering every optional-field variant). Every fixture entry MUST parse
through :func:`parse_ui_intent` — a failure here means the widget and the
per-intent schemas in ``assist/commerce/intents.py`` have drifted, which
is exactly the class of bug that shipped two 422s to shoppers.

NOTE: the fixture file must stay in sync with the loom repo copy at
``packages/client-sdk/src/lib/chat/__fixtures__/intent_payloads.json`` —
update both (and both repos' contract tests) when payloads change.

Also guards the ``extra="ignore"`` payload policy: an unknown extra key
(a newer widget talking to an older server) parses fine, is dropped from
the model, and logs one structured WARNING naming the dropped keys —
never their values.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from app.ai.voice.agents.breeze_buddy.assist.commerce import intents as ci
from app.ai.voice.agents.breeze_buddy.chat.intents import router as ir

# Importing ``ci`` registered the commerce policies; keep the public
# loader idempotent-checked like the sibling test module.
ir.ensure_flavor_intents(["commerce"])

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "intent_payloads.json"

PAYLOAD_MODELS = {
    "add_to_cart": ci.AddToCartPayload,
    "remove_line": ci.RemoveLinePayload,
    "set_qty": ci.SetQtyPayload,
    "view_product": ci.ViewProductPayload,
    "enrich_product": ci.EnrichProductPayload,
    "checkout": ci.CheckoutPayload,
}


def _load_fixtures() -> Dict[str, List[Dict[str, Any]]]:
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _fixture_cases() -> List[Tuple[str, int, Dict[str, Any]]]:
    return [
        (intent, i, payload)
        for intent, payloads in sorted(_load_fixtures().items())
        for i, payload in enumerate(payloads)
    ]


def test_fixtures_cover_every_registered_commerce_intent():
    """One fixture list per registered intent — a new intent without a
    captured emission (or a fixture for a dropped intent) fails here."""
    assert set(_load_fixtures()) == set(ir.INTENT_POLICY)
    assert set(_load_fixtures()) == set(PAYLOAD_MODELS)


@pytest.mark.parametrize(
    "intent,variant,payload",
    _fixture_cases(),
    ids=[f"{intent}[{i}]" for intent, i, _ in _fixture_cases()],
)
def test_widget_emission_parses(intent: str, variant: int, payload: Dict[str, Any]):
    """Every captured widget payload parses via parse_ui_intent with no
    IntentValidationError AND with every sent key accepted (nothing
    silently dropped for TODAY's known emissions)."""
    parsed = ir.parse_ui_intent(
        {"intent": intent, "component_id": "pg1", "payload": dict(payload)},
        enabled_flavors={"commerce"},
    )
    assert isinstance(parsed.payload, PAYLOAD_MODELS[intent])
    # Known fields must be modeled explicitly — a fixture key missing from
    # the schema would be dropped (drift WARNING in prod); fail loudly here.
    assert set(payload) <= set(PAYLOAD_MODELS[intent].model_fields)
    # Round-trip: the modeled values match what the widget sent.
    dumped = parsed.payload.model_dump(exclude_none=True)
    for key, value in payload.items():
        assert dumped[key] == value


def test_unknown_extra_key_parses_and_logs_drop(monkeypatch):
    """extra="ignore" regression guard: a future-widget extra key must not
    422; it is dropped and one WARNING names the dropped keys (values are
    never logged)."""
    warnings: List[str] = []

    class _Recorder:
        def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
            warnings.append(message)

    monkeypatch.setattr(ci, "logger", _Recorder())

    parsed = ir.parse_ui_intent(
        {
            "intent": "view_product",
            "component_id": "pg1",
            "payload": {
                "product_id": "gid://shopify/Product/1",
                "title": "Trail Socks",
                "url": "https://shop.example/products/socks",
                "future_field": "SECRET-VALUE",
                "another_new_key": 42,
            },
        },
        enabled_flavors={"commerce"},
    )
    assert isinstance(parsed.payload, ci.ViewProductPayload)
    assert parsed.payload.url == "https://shop.example/products/socks"
    assert not hasattr(parsed.payload, "future_field")
    assert "future_field" not in parsed.payload.model_dump()

    assert len(warnings) == 1  # single structured WARNING per request
    assert "ViewProductPayload" in warnings[0]
    assert "future_field" in warnings[0]
    assert "another_new_key" in warnings[0]
    assert "SECRET-VALUE" not in warnings[0]  # key names only, never values
    assert "42" not in warnings[0]


def test_required_field_validation_stays_strict():
    """The compat policy loosens UNKNOWN keys only — a missing/invalid
    required field is still a typed 422."""
    with pytest.raises(ir.IntentValidationError) as exc:
        ir.parse_ui_intent(
            {
                "intent": "add_to_cart",
                "component_id": "pg1",
                "payload": {"qty": 1, "someday_field": True},
            },
            enabled_flavors={"commerce"},
        )
    assert exc.value.detail["code"] == "invalid_intent_payload"
    assert any(e["loc"] == "variant_id" for e in exc.value.detail["errors"])


# ---------------------------------------------------------------------------
# Cross-repo checksum pin
# ---------------------------------------------------------------------------

# sha256 of fixtures/intent_payloads.json. The loom repo pins the SAME
# constant over ITS byte-identical copy (client-sdk
# src/lib/chat/__tests__/intent-fixture-checksum.test.ts). Editing either
# copy fails that repo's CI until the constant is bumped — and bumping it
# is the reviewer's cue to update the other repo in the same change.
INTENT_FIXTURE_SHA256 = (
    "98be2907cebd786f9a5b85d62e6628f9d030e04479fe5f9375d9b9f85fd061ac"
)


def test_fixture_checksum_matches_cross_repo_pin():
    import hashlib

    digest = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    assert digest == INTENT_FIXTURE_SHA256, (
        "intent_payloads.json changed — update INTENT_FIXTURE_SHA256 here AND "
        "the identical pin + fixture copy in the loom repo "
        "(packages/client-sdk/src/lib/chat/__fixtures__/ + "
        "__tests__/intent-fixture-checksum.test.ts)."
    )
