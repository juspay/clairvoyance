"""Research notes sorted into the store's form: what lands where, what is dropped."""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.assist.engine import fields
from app.ai.voice.agents.breeze_buddy.assist.verticals import registry

STORE = registry.resolve("commerce").fields


def test_notes_fill_the_fields_they_name() -> None:
    notes = [
        ("brand_line", "Kosha: merino for Indian winters"),
        ("brand_line", "A second line is not a second brand"),
        ("offer_items", "20% off thermals"),
        ("offer_items", "  20%   off thermals "),
        ("offer_items", "Free socks over 999"),
        ("email", ""),
        ("not_a_field", "dropped"),
    ]
    assert fields.fields_from_notes(notes, STORE) == {
        "brand_line": ["Kosha: merino for Indian winters"],
        "offer_items": ["20% off thermals", "Free socks over 999"],
    }


def test_returns_delivery_and_questions_land_in_hidden_policies() -> None:
    notes = [
        ("returns", "Easy 7-day returns"),
        ("delivery", "Ships in 2-4 days"),
        ("faq", "Do you ship abroad? Not yet."),
    ]
    found = fields.fields_from_notes(notes, STORE)
    assert found == {
        "policies": [
            "Easy 7-day returns",
            "Ships in 2-4 days",
            "Do you ship abroad? Not yet.",
        ]
    }
    policies = STORE.spec("policies")
    assert policies is not None and policies.hidden


def test_values_are_capped_and_the_merchant_only_fields_stay_empty() -> None:
    notes = [("trust_items", f"reason {n}") for n in range(40)]
    notes += [("assistant_name", "Set by research"), ("email", "x" * 900)]
    found = fields.fields_from_notes(notes, STORE)
    assert len(found["trust_items"]) == fields.MAX_VALUES_PER_FIELD
    assert len(found["email"][0]) == fields.MAX_VALUE_CHARS
    assert "assistant_name" not in found
    keys = [spec.key for spec in STORE.specs()]
    assert len(keys) == len(set(keys))


def test_a_text_field_joins_its_notes_into_one_paragraph() -> None:
    notes = [
        ("what_we_sell", "Polos"),
        ("what_we_sell", "Shirts."),
        ("what_we_sell", "polos"),
        ("what_we_sell", "x" * 490),
        ("vocabulary", "Quiet luxury"),
    ]
    assert fields.fields_from_notes(notes, STORE) == {
        "what_we_sell": ["Polos, Shirts"],
        "vocabulary": ["Quiet luxury"],
    }
