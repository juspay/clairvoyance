"""The one decode engine (event-catalog.md §One decode engine, two spec
sources): a spec in, handles + facts + variables out — the same function
for a code CatalogEntry and a registered vendor row.

The Shopify cases carried over from the imperative extractor it replaced:
same letters, same answers, now read by declared paths.
"""

import json
from pathlib import Path
from typing import Any, Dict

from app.crm.record import catalog
from app.crm.record.extractors import EXTRACTORS, SPEC_MODULES, engine, shopify
from app.crm.record.extractors.engine import EMPTY_SPEC, DecodeSpec, spec_for_entry
from app.crm.record.schemas import CatalogEntry, CatalogField


def _shopify(topic: str) -> DecodeSpec:
    spec = catalog.code_spec("shopify", topic)
    assert spec is not None, topic
    return spec


ORDER = _shopify("orders/create")


def _fixture(name: str) -> Dict[str, Any]:
    """One recorded Shopify letter — the fixture IS the payload."""
    return json.loads(
        (Path(__file__).parent / "fixtures" / "shopify" / f"{name}.json").read_text()
    )


CHECKOUT = _shopify("checkouts/update")


def _entry(*fields: CatalogField) -> CatalogEntry:
    return CatalogEntry(
        source="x", topic="t", label="T", group="X", layer="code", fields=list(fields)
    )


# --- the spec, built from either layer ----------------------------------------


def test_an_entry_says_who_its_letters_are_about() -> None:
    """The engine's vocabulary (Extracted.about) is the entry's word: a
    merchant-level topic (a template review, an account notice) decodes
    with about="merchant" — no handle, no quarantine, NULL customer by
    design — and the default stays "customer" for every existing entry."""
    review = CatalogEntry(
        source="whatsapp",
        topic="template.status",
        label="Template review",
        group="WhatsApp",
        layer="code",
        about="merchant",
        fields=[
            CatalogField(path="payload.event", type="text", label="E", variable=True)
        ],
    )
    spec = spec_for_entry(review, {})
    assert spec.about == "merchant"
    out = engine.extract({"event": "APPROVED"}, spec)
    assert out.about == "merchant" and out.handles == {}
    assert out.variables == {"event": "APPROVED"}

    assert spec_for_entry(_entry(), {}).about == "customer"
    assert EMPTY_SPEC.about == "customer"
    assert (
        engine.extract({"customer_mobile_number": "+919999999999"}, EMPTY_SPEC).about
        == "customer"
    )


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
    modules = {module.SOURCE: module for module in SPEC_MODULES}
    for key, entry in catalog.CATALOG.items():
        declared = {f.path for f in entry.fields if f.derived}
        assert declared == set(catalog.DERIVE[key]), key
        for name in declared:
            assert catalog.DERIVE[key][name] is modules[key[0]].DERIVERS[name]


def _spec_dict(spec: DecodeSpec) -> Dict[str, Any]:
    return {"identity": spec.identity, "variables": spec.variables}


# --- a declared list becomes one sentence ------------------------------------


def _listed(path: str, fmt: Any = None) -> CatalogEntry:
    return _entry(
        CatalogField(
            path=path, type="list", label="Items", variable=True, item_format=fmt
        )
    )


def test_a_declared_list_is_joined_into_one_scalar() -> None:
    """The letter keeps every key on the event row; the run carries the
    sentence a template can actually render."""
    spec = spec_for_entry(_listed("payload.line_items.title"), {})
    assert spec.lists == {"title": None}
    payload = {"line_items": [{"title": "Kurta"}, {"title": "Dupatta"}]}
    assert engine.extract(payload, spec).variables == {"title": "Kurta, Dupatta"}


def test_an_item_format_pairs_the_keys_of_one_line() -> None:
    """The thing a path alone cannot say: `line_items.title` and
    `line_items.quantity` are two parallel lists ("Kurta, Cap" beside
    "1, 2"), never "Kurta x1, Cap x2"."""
    spec = spec_for_entry(
        _listed("payload.line_items", "{title} x{quantity} -\u20b9{price}"), {}
    )
    payload = {
        "line_items": [
            {"title": "Kurta", "quantity": 1, "price": "1199.00", "sku": "K-1"},
            {"title": "Cap", "quantity": 2, "price": "150.00", "sku": "C-2"},
        ]
    }
    assert engine.extract(payload, spec).variables == {
        "line_items": "Kurta x1 -\u20b91199.00, Cap x2 -\u20b9150.00"
    }


def test_a_list_of_bare_scalars_needs_no_format() -> None:
    spec = spec_for_entry(_listed("payload.tags"), {})
    assert engine.extract({"tags": ["vip", "new"]}, spec).variables == {
        "tags": "vip, new"
    }


def test_a_line_missing_a_blank_is_skipped_whole() -> None:
    """Not " x2". A half-formed line is corruption that looks delivered;
    better to name three items than four badly."""
    spec = spec_for_entry(_listed("payload.xs", "{title} x{quantity}"), {})
    payload = {
        "xs": [{"title": "Kurta"}, {"quantity": 3}, {"title": "Cap", "quantity": 1}]
    }
    assert engine.extract(payload, spec).variables == {"xs": "Cap x1"}


def test_nothing_renderable_is_no_variable_at_all() -> None:
    """None, not "" — a template mapping it parks by name, which is honest,
    where an empty blank sends a message with a hole in it."""
    spec = spec_for_entry(_listed("payload.xs", "{title}"), {})
    for payload in ({"xs": []}, {"xs": [{}]}, {"xs": "not a list"}, {}):
        assert engine.extract(payload, spec).variables == {}


def test_a_long_cart_is_truncated_with_the_overflow_counted() -> None:
    """Truncating at the join is the point: over the ceiling the value would
    be dropped by the scalar gate and the send would park on a blank whose
    cause is two modules away."""
    spec = spec_for_entry(_listed("payload.xs", "{title}"), {})
    payload = {"xs": [{"title": f"Item number {i}"} for i in range(200)]}
    rendered = engine.extract(payload, spec).variables["xs"]
    assert len(rendered) <= engine.VARIABLE_MAX_CHARS
    assert rendered.startswith("Item number 0, Item number 1, ")
    assert rendered.endswith(" more")
    # one oversized line is omitted, and still counted
    assert engine.join_list(["a" * 300, "b"]) == "+2 more"


def test_shopify_offers_every_phrasing_of_the_cart() -> None:
    """Three declared blanks over one array, so a plan picks the phrasing
    its template needs — and they are DERIVED rather than three `list` fields
    on payload.line_items, because a variable is named for its path\'s last
    segment and three of those would all be called `line_items`."""
    variables = engine.extract(_fixture("orders_create"), ORDER).variables
    assert variables["items"] == "Air Runner Sneakers, Ankle Socks (3 pack)"
    assert variables["items_qty"] == ("Air Runner Sneakers x1, Ankle Socks (3 pack) x2")
    # The money carries the ORDER's currency, never a hard-coded symbol.
    assert variables["items_priced"] == (
        "Air Runner Sneakers = 2499.00 INR x1, Ankle Socks (3 pack) = 299.00 INR x2"
    )
    # every phrasing is a distinct blank — the collision this shape avoids
    assert len({variables[k] for k in ("items", "items_qty", "items_priced")}) == 3
    assert variables["payment_gateway_names"] == "Cash on Delivery (COD)"
    assert variables["order_status_url"].startswith("https://")
    assert variables["city"] == "Bengaluru"
    # the derived pair stays beside it — live plans template on them
    assert variables["items_count"] == 2
    assert variables["first_item_name"] == "Air Runner Sneakers"


def test_the_cart_is_priced_in_the_order_s_own_currency() -> None:
    """The symbol is a fact of the LETTER, not of the format: a hard-coded ₹
    renders a USD store's cart at the wrong price. Same cart, three stores —
    the phrasing is identical and only the code moves."""
    payload = _fixture("orders_create")
    payload["line_items"] = [{"title": "Kurta", "quantity": 2, "price": "100.00"}]

    payload["currency"] = "USD"
    assert (
        engine.extract(payload, ORDER).variables["items_priced"]
        == "Kurta = 100.00 USD x2"
    )

    # A multi-currency store sends BOTH, and the shop's code wins: REST
    # spells line_items[].price in the shop's currency, so labelling it
    # "USD" would price an INR number in dollars.
    payload["currency"] = "INR"
    payload["presentment_currency"] = "USD"
    assert (
        engine.extract(payload, ORDER).variables["items_priced"]
        == "Kurta = 100.00 INR x2"
    )

    # `presentment_currency` is read only when the shop's own is absent.
    del payload["currency"]
    payload["presentment_currency"] = "aed"
    assert (
        engine.extract(payload, ORDER).variables["items_priced"]
        == "Kurta = 100.00 AED x2"
    )

    # Neither: the bare number. The currency rides in the derived VALUE, not
    # in a {currency} blank of its own — render_item skips a line WHOLE when
    # a blank is missing, so a blank would have emptied the cart instead.
    del payload["presentment_currency"]
    assert (
        engine.extract(payload, ORDER).variables["items_priced"] == "Kurta = 100.00 x2"
    )


def test_the_shipping_address_is_one_sentence_with_no_holes_in_it() -> None:
    """Six parts a template author would otherwise place by hand, in the
    order a label is read, with the absent ones DROPPED — never ", , "."""
    assert (
        engine.extract(_fixture("orders_create"), ORDER).variables["shipping_address"]
        == "Priya Sharma, 12 MG Road, Bengaluru, Karnataka, 560001"
    )

    # Shopify's newer shape sends a joined `name` and an explicit null
    # address2. Both are read, and the null simply does not appear.
    payload = _fixture("orders_create")
    payload["shipping_address"] = {
        "zip": "560095",
        "city": "Bangalore",
        "name": "Swaroop Varma",
        "company": None,
        "country": "India",
        "address1": "A32",
        "address2": None,
        "latitude": 12.938781,
        "province": "Karnataka",
        "last_name": "Varma",
        "longitude": 77.62067689999999,
        "first_name": "Swaroop",
        "country_code": "IN",
        "province_code": "KA",
    }
    assert (
        engine.extract(payload, ORDER).variables["shipping_address"]
        == "Swaroop Varma, A32, Bangalore, Karnataka, 560095"
    )

    # A letter with no address at all says nothing, rather than saying ", ".
    payload.pop("shipping_address")
    assert "shipping_address" not in engine.extract(payload, ORDER).variables


def test_a_yes_no_is_declared_for_filtering_and_never_as_a_blank() -> None:
    """send_variables refuses a bool, so a boolean that were a variable
    would park the run at fire time. No code-layer boolean is one."""
    for module in SPEC_MODULES:
        for entry in module.ENTRIES:
            for f in entry.fields:
                if f.type == "boolean":
                    assert not f.variable, f"{entry.topic}: {f.path}"


def test_a_line_with_no_price_is_left_out_of_the_sentence() -> None:
    """The missing-blank law reaching the computed key too: `unit_price` is
    assembled by the deriver, not sent by Shopify, and a line the format
    cannot complete is skipped whole rather than rendered half."""
    entry = _entry(
        CatalogField(
            path="line_items", type="text", label="x", derived=True, variable=True
        )
    )
    derive = {
        "line_items": catalog.derive_for("shopify", "orders/create")["items_priced"]
    }
    spec = spec_for_entry(entry, derive)
    payload = {
        "line_items": [
            {"title": "Kurta", "quantity": 2, "price": "100.00"},
            {"title": "Broken", "quantity": 1},
        ]
    }
    # "Broken" carries no price, so it has no `unit_price` and drops out.
    # This letter also names no `currency`, so the money degrades to the bare
    # number — a cart WITHOUT a currency code, never an EMPTY cart.
    assert engine.extract(payload, spec).variables == {
        "line_items": "Kurta = 100.00 x2"
    }


def test_a_list_inside_a_list_is_just_another_step() -> None:
    """Real letters nest: an order's applications, each with its own offers.
    A path that stopped at the first array could name the application but
    never the offer, so every array crossed is mapped over and flattened —
    which is the honest answer for a blank, since a blank is ONE string and
    cannot carry which application an offer came from anyway."""
    payload = {
        "loanApplications": [
            {
                "lenderName": "FINNABLE",
                "offers": [
                    {
                        "sanctionedAmount": "9000.00",
                        "duration": "6",
                        "emiType": "NO_COST_EMI_WITH_DISCOUNT",
                    },
                    {
                        "sanctionedAmount": "8500.00",
                        "duration": "9",
                        "emiType": "STANDARD",
                    },
                ],
            },
            {
                "lenderName": "DMI",
                "offers": [
                    {
                        "sanctionedAmount": "7000.00",
                        "duration": "12",
                        "emiType": "STANDARD",
                    }
                ],
            },
        ]
    }

    def one(path: str, fmt: Any = None) -> Any:
        entry = _entry(
            CatalogField(
                path=path, type="list", label="X", variable=True, item_format=fmt
            )
        )
        return engine.extract(payload, spec_for_entry(entry, {})).variables

    # one level down
    assert one("payload.loanApplications.lenderName") == {"lenderName": "FINNABLE, DMI"}
    # two levels down, flattened across both applications
    assert one("payload.loanApplications.offers.sanctionedAmount") == {
        "sanctionedAmount": "9000.00, 8500.00, 7000.00"
    }
    # …and the nested elements are what an item_format reads
    assert one(
        "payload.loanApplications.offers",
        "{duration} - {emiType} - {sanctionedAmount}",
    ) == {
        "offers": (
            "6 - NO_COST_EMI_WITH_DISCOUNT - 9000.00, "
            "9 - STANDARD - 8500.00, "
            "12 - STANDARD - 7000.00"
        )
    }
