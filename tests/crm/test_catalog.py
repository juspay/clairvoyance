"""The event catalog's pin tests (design/event-catalog.md §Ownership): the
four-part square a new source/topic must ship — spec (the engine
decodes it) · fixtures · catalog entry (· ingress) — enforced so a
half-added entry fails CI.

Fixtures under tests/crm/fixtures/<source>/<topic>.json are recorded
payloads (the Shopify one is document-shaped until nautilus#195's shadow
records a real letter; the pin holds either way). They double as the
catalog's conformance test: every declared non-derived field must resolve
against at least one fixture, so provider drift fails HERE, not in prod.
"""

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.crm.record import catalog
from app.crm.record.extractors import engine
from app.crm.record.schemas import CatalogField, SchemaRegistration
from app.crm.shared.predicate import Condition, matches

FIXTURES = Path(__file__).parent / "fixtures"


def _fixtures_for(source: str, topic: str) -> List[Dict[str, Any]]:
    folder = FIXTURES / source
    name = topic.replace("/", "_").replace(".", "_")
    # sorted: glob order is the filesystem's (Linux CI once handed the
    # signed-in variant first), and callers index into this list.
    return [
        json.loads(p.read_text())
        for p in sorted(folder.glob("*.json"))
        if p.stem.startswith(name)
    ]


# --- The square -------------------------------------------------------------


def test_every_code_entry_has_fixtures_and_every_field_appears_in_one() -> None:
    """Every declared path resolves against a recorded letter of its topic;
    every FALLBACK path against a recorded letter of its source (Shopify
    keeps a phone in four homes — each must be seen in the wild once)."""
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    for (source, topic), entry in catalog.CATALOG.items():
        payloads = _fixtures_for(source, topic)
        assert payloads, f"{source}/{topic}: no fixture under tests/crm/fixtures"
        by_source.setdefault(source, []).extend(payloads)
        derive = catalog.derive_for(source, topic)
        for field in entry.fields:
            assert any(
                engine.field_value(p, field.path, derive) is not None for p in payloads
            ), f"{source}/{topic}: declared field {field.path} appears in no fixture"
    for (source, topic), entry in catalog.CATALOG.items():
        for field in entry.fields:
            for path in field.fallbacks:
                assert any(
                    engine.field_value(p, path) not in (None, "")
                    for p in by_source[source]
                ), f"{source}: fallback {path} appears in no recorded letter"


def test_the_engine_finds_the_person_in_every_recorded_letter() -> None:
    """The extractor half of the square: a code entry whose spec cannot
    attribute its own fixtures would quarantine every real letter."""
    for (source, topic), entry in catalog.CATALOG.items():
        spec = catalog.code_spec(source, topic)
        assert spec is not None
        for payload in _fixtures_for(source, topic):
            extracted = engine.extract(payload, spec)
            assert extracted.handles, f"{source}/{topic}: a fixture yields no handle"


def test_variable_names_are_unique_within_an_entry() -> None:
    # {placeholder} is the last path segment (or the derived name): two
    # declared variables ending in the same word would fill one slot.
    for key, entry in catalog.CATALOG.items():
        names = [engine.variable_name(f.path) for f in entry.fields if f.variable]
        assert len(names) == len(set(names)), (key, names)


def test_every_derived_field_has_its_deriver_and_vice_versa() -> None:
    for key, entry in catalog.CATALOG.items():
        declared = {f.path for f in entry.fields if f.derived}
        assert declared == set(catalog.DERIVE.get(key, {})), key


def test_ops_come_from_type_and_phone_is_never_filterable() -> None:
    for entry in catalog.code_entries():
        for field in entry.fields:
            assert field.ops == catalog.OPS_BY_TYPE[field.type]
            if field.type == "phone":
                assert field.ops == []
    assert set(catalog.OPS_BY_TYPE) == {
        "text",
        "number",
        "choice",
        "boolean",
        "datetime",
        "phone",
    }


# --- Shopify orders/create through the grammar ----------------------------


def test_shopify_fixture_answers_the_worked_example() -> None:
    # the recorded COD order by name — its variants (guest, signed-in)
    # answer differently and are pinned by the coverage tests above
    payload = json.loads((FIXTURES / "shopify" / "orders_create.json").read_text())
    derive = catalog.derive_for("shopify", "orders/create")
    lookup = lambda path: engine.field_value(payload, path, derive)  # noqa: E731
    assert matches(
        [
            Condition(field="payload.financial_status", op="is", value="pending"),
            Condition(field="payload.gateway", op="is", value="Cash on Delivery (COD)"),
            Condition(field="payload.total_price", op=">", value=1000),
            Condition(field="items_count", op="=", value=2),
        ],
        lookup,
    )
    assert lookup("first_item_name") == "Air Runner Sneakers"
    assert lookup("customer_name") == "Priya Sharma"
    # Shopify's top-level customer.phone is null on this recorded order —
    # the DECLARED fallback (default_address) is where the engine finds it.
    assert lookup("payload.customer.phone") is None
    spec = catalog.code_spec("shopify", "orders/create")
    assert spec is not None
    assert engine.extract(payload, spec).handles["phone"] == "+919876543210"
    assert lookup("payload.customer.missing") is None
    assert lookup("payload.customer.phone.deeper") is None


def test_canonical_path_keeps_bare_entry_keys_working() -> None:
    assert catalog.canonical_path("order_id") == "payload.order_id"
    assert catalog.canonical_path("payload.order_id") == "payload.order_id"
    assert catalog.canonical_path("items_count") == "items_count"


# --- Registration validator (T24) --------------------------------------------


def _reg(fields: List[Dict[str, Any]]) -> SchemaRegistration:
    return SchemaRegistration(
        source="flipkart",
        topic="order.created",
        label="Order placed",
        fields=[CatalogField.model_validate(f) for f in fields],
    )


def test_registration_accepts_the_nammayatri_shape() -> None:
    problems = catalog.validate_registration(
        _reg(
            [
                {
                    "path": "payload.ride_id",
                    "type": "text",
                    "label": "Ride ID",
                    "keyable": True,
                },
                {
                    "path": "payload.rider_phone",
                    "type": "phone",
                    "label": "Phone",
                    "identity": "phone",
                },
                {
                    "path": "payload.fare",
                    "type": "number",
                    "label": "Fare",
                    "variable": True,
                },
                {
                    "path": "payload.cancelled_by",
                    "type": "choice",
                    "label": "Cancelled by",
                    "values": ["driver", "customer", "system"],
                },
            ]
        )
    )
    assert problems == []


@pytest.mark.parametrize(
    "field, fragment",
    [
        ({"path": "ride_id", "type": "text", "label": "x"}, "path must be payload."),
        (
            {"path": "payload.a-b", "type": "text", "label": "x"},
            "path must be payload.",
        ),
        (
            {"path": "payload.x", "type": "choice", "label": "x"},
            "choice needs its values",
        ),
        (
            {"path": "payload.x", "type": "text", "label": "x", "values": ["a"]},
            "only choice",
        ),
        (
            {"path": "payload.x", "type": "boolean", "label": "x", "keyable": True},
            "keyable needs",
        ),
        (
            {"path": "payload.x", "type": "text", "label": "x", "identity": "phone"},
            "must be type phone",
        ),
        (
            {"path": "payload.x", "type": "phone", "label": "x"},
            "identity:phone or nothing",
        ),
        (
            {"path": "payload.x", "type": "text", "label": "x", "derived": True},
            "code-layer only",
        ),
        (
            {
                "path": "payload.x",
                "type": "phone",
                "label": "x",
                "identity": "phone",
                "fallbacks": ["payload.y"],
            },
            "fallbacks are code-layer only",
        ),
        (
            {"path": "payload.x", "type": "text", "label": "x", "identity": "email"},
            "identity role must be one of phone | name",
        ),
    ],
)
def test_registration_refuses_each_law_break(
    field: Dict[str, Any], fragment: str
) -> None:
    problems = catalog.validate_registration(_reg([field]))
    assert any(fragment in p for p in problems), problems


def test_registration_refuses_unknown_type_at_the_shape() -> None:
    with pytest.raises(Exception):
        CatalogField.model_validate(
            {"path": "payload.x", "type": "money", "label": "x"}
        )


def test_registration_refuses_two_fields_for_one_identity_role() -> None:
    problems = catalog.validate_registration(
        _reg(
            [
                {
                    "path": "payload.a",
                    "type": "phone",
                    "label": "a",
                    "identity": "phone",
                },
                {
                    "path": "payload.b",
                    "type": "phone",
                    "label": "b",
                    "identity": "phone",
                },
            ]
        )
    )
    assert any("already taken" in p for p in problems)


def test_etag_changes_with_content_not_with_counts() -> None:
    entries = catalog.code_entries()
    a = catalog.etag_for(entries)
    assert a == catalog.etag_for(
        [e.model_copy(update={"seen_7d": 99}) for e in entries]
    )
    changed = [e.model_copy(update={"label": "renamed"}) for e in entries]
    assert a != catalog.etag_for(changed)


def test_type_guess_from_samples() -> None:
    assert catalog._guess_type("number", [1, 2]) == "number"
    assert catalog._guess_type("boolean", [True]) == "boolean"
    assert catalog._guess_type("string", ["+919876543210", "9876543210"]) == "phone"
    assert catalog._guess_type("string", ["2026-09-01T10:00:00Z"]) == "datetime"
    assert catalog._guess_type("string", ["COD", "UPI"]) == "text"
