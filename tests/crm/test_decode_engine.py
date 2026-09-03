"""The one decode engine (event-catalog.md §One decode engine, two spec
sources): a spec in, handles + facts + variables out — the same function
for a code CatalogEntry and a registered vendor row.

The Shopify cases carried over from the imperative extractor it replaced:
same letters, same answers, now read by declared paths.
"""

from typing import Any, Dict

from app.crm.record import catalog
from app.crm.record.extractors import EXTRACTORS, engine, shopify, whatsapp
from app.crm.record.extractors.engine import EMPTY_SPEC, DecodeSpec, spec_for_entry
from app.crm.record.schemas import CatalogEntry, CatalogField


def _shopify(topic: str) -> DecodeSpec:
    spec = catalog.code_spec("shopify", topic)
    assert spec is not None, topic
    return spec


ORDER = _shopify("orders/create")
CHECKOUT = _shopify("checkouts/update")


def _entry(*fields: CatalogField) -> CatalogEntry:
    return CatalogEntry(
        source="x", topic="t", label="T", group="X", layer="code", fields=list(fields)
    )


# --- the spec, built from either layer ----------------------------------------


def test_a_spec_is_the_same_shape_from_code_and_from_a_registration() -> None:
    registered = _entry(
        CatalogField(
            path="payload.rider.phone", type="phone", label="P", identity="phone"
        ),
        CatalogField(
            path="payload.rider.name", type="text", label="N", identity="name"
        ),
        CatalogField(path="payload.fare", type="number", label="F", variable=True),
    )
    spec = spec_for_entry(registered, {})
    assert spec.identity == {
        "phone": ["payload.rider.phone"],
        "name": ["payload.rider.name"],
    }
    assert spec.variables == {"fare": "payload.fare"}
    # The code layer's spec is built by the same function.
    assert ORDER.identity["phone"] == shopify.PHONE_PATHS


def test_fallbacks_follow_the_field_in_order_and_deprecated_fields_drop_out() -> None:
    entry = _entry(
        CatalogField(
            path="payload.a",
            type="phone",
            label="P",
            identity="phone",
            fallbacks=["payload.b", "payload.c"],
        ),
        CatalogField(
            path="payload.old", type="text", label="O", variable=True, deprecated=True
        ),
    )
    spec = spec_for_entry(entry, {})
    assert spec.identity["phone"] == ["payload.a", "payload.b", "payload.c"]
    assert spec.variables == {}


def test_variable_names_are_the_last_path_segment_or_the_derived_name() -> None:
    assert engine.variable_name("payload.customer.first_name") == "first_name"
    assert engine.variable_name("payload.total_price") == "total_price"
    assert engine.variable_name("customer_name") == "customer_name"


# --- decoding Shopify by its declared spec -------------------------------------


def test_the_nested_customer_phone_beats_the_shipping_contact() -> None:
    # The relay forwards Shopify's body unopened: the phone arrives nested
    # and the top-level one is usually null.
    extracted = engine.extract(
        {
            "phone": None,
            "customer": {
                "first_name": "Priya",
                "last_name": "Sharma",
                "phone": "+91 98765 43210",
            },
            "shipping_address": {"phone": "9999999999"},
        },
        ORDER,
    )
    assert extracted.handles["phone"] == "+919876543210"  # normalized
    assert extracted.facts == {"name": "Priya Sharma"}


def test_a_guest_checkout_falls_back_to_the_shipping_contact() -> None:
    # No customer object at all — the last fallback in the declared list.
    extracted = engine.extract(
        {
            "shipping_address": {
                "first_name": "Rohan",
                "last_name": "Mehta",
                "phone": "9876543210",
            }
        },
        ORDER,
    )
    assert extracted.handles["phone"] == "+919876543210"
    assert extracted.facts == {"name": "Rohan Mehta"}
    assert "shopify_customer_id" not in extracted.handles


def test_the_default_address_is_the_returning_shoppers_phone_home() -> None:
    extracted = engine.extract(
        {"customer": {"phone": None, "default_address": {"phone": "98765 43210"}}},
        ORDER,
    )
    assert extracted.handles["phone"] == "+919876543210"


def test_a_name_is_never_invented() -> None:
    # A placeholder would reach assert_facts as a real claim and overwrite
    # what we actually know. Absent is absent.
    extracted = engine.extract({"customer": {"phone": "9876543210"}}, ORDER)
    assert extracted.facts == {}
    assert "customer_name" not in extracted.variables


def test_an_unusable_phone_is_skipped_not_written() -> None:
    extracted = engine.extract({"customer": {"phone": "n/a"}}, ORDER)
    assert "phone" not in extracted.handles


def test_email_and_customer_id_are_handles_too() -> None:
    extracted = engine.extract(
        {
            "customer": {
                "id": 77,
                "email": "  Priya@Example.COM  ",
                "phone": "9876543210",
            }
        },
        ORDER,
    )
    assert extracted.handles["email"] == "priya@example.com"
    assert extracted.handles["shopify_customer_id"] == "77"


def test_a_phoneless_checkout_frame_resolves_by_the_customer_id() -> None:
    # The early checkouts/update frames carry no phone yet — she types it
    # later — but they carry Shopify's customer id.
    extracted = engine.extract({"customer": {"id": 77}, "token": "ck-1"}, CHECKOUT)
    assert extracted.handles == {"shopify_customer_id": "77"}
    assert extracted.variables["token"] == "ck-1"


def test_declared_variables_come_out_named_for_templates() -> None:
    extracted = engine.extract(
        {
            "id": 881,
            "name": "#881",
            "cart_token": "c1-abc",
            "total_price": "2499.00",
            "customer": {
                "first_name": "Priya",
                "last_name": "Sharma",
                "phone": "9876543210",
            },
            "line_items": [{"title": "Sneakers"}, {"title": "Socks"}],
            "confirmed": True,
        },
        ORDER,
    )
    v = extracted.variables
    assert v["customer_name"] == "Priya Sharma"  # derived, nested, template-ready
    assert v["first_name"] == "Priya"
    assert v["total_price"] == "2499.00" and v["cart_token"] == "c1-abc"
    assert v["items_count"] == 2 and v["first_item_name"] == "Sneakers"
    assert "confirmed" not in v  # undeclared scalars never become variables


# --- the flat shape beneath every spec ------------------------------------------


def test_the_flat_shape_applies_with_no_spec_at_all() -> None:
    extracted = engine.extract(
        {"customer_mobile_number": "+919999999999", "customer_name": "Asha"}, EMPTY_SPEC
    )
    assert extracted.handles == {"phone": "+919999999999"}
    assert extracted.facts == {"name": "Asha"}
    assert extracted.variables == {}


def test_a_declared_path_wins_over_the_standard_key() -> None:
    spec = DecodeSpec(identity={"phone": ["payload.rider.phone"]})
    extracted = engine.extract(
        {"customer_mobile_number": "+911111111111", "rider": {"phone": "9876543210"}},
        spec,
    )
    assert extracted.handles["phone"] == "+919876543210"


def test_a_missing_declared_path_leaves_the_standard_key_standing() -> None:
    spec = DecodeSpec(identity={"phone": ["payload.rider.phone"]})
    extracted = engine.extract({"customer_mobile_number": "+911111111111"}, spec)
    assert extracted.handles["phone"] == "+911111111111"


# --- one engine: a code-catalog source never also decodes by hand -------------


def test_shopify_is_a_spec_not_an_imperative_extractor() -> None:
    assert "shopify" not in EXTRACTORS
    for source, _ in catalog.CATALOG:
        assert source not in EXTRACTORS, f"{source} has two readers of one payload"


def test_every_catalog_derived_field_is_provided_by_its_spec_module() -> None:
    modules = {shopify.SOURCE: shopify, whatsapp.SOURCE: whatsapp}
    for key, entry in catalog.CATALOG.items():
        declared = {f.path for f in entry.fields if f.derived}
        assert declared == set(catalog.DERIVE[key]), key
        for name in declared:
            assert catalog.DERIVE[key][name] is modules[key[0]].DERIVERS[name]


def _spec_dict(spec: DecodeSpec) -> Dict[str, Any]:
    return {"identity": spec.identity, "variables": spec.variables}
