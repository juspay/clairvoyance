"""Research notes sorted into the store's form: what lands where, what is dropped."""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.assist.verticals import fields, registry

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


def test_returns_delivery_and_questions_each_keep_their_own_facts() -> None:
    # Many questions never push out the delivery facts.
    notes = [("faq", f"Q: question {n}? A: answer") for n in range(12)]
    notes += [("returns", "Easy 7-day returns"), ("delivery", "Ships in 2-4 days")]
    found = fields.fields_from_notes(notes, STORE)
    assert len(found["faq"]) == fields._MAX_VALUES_PER_FIELD
    assert found["returns"] == ["Easy 7-day returns"]
    assert found["delivery"] == ["Ships in 2-4 days"]


def test_a_value_can_never_open_a_template_section_or_a_heading() -> None:
    notes = [
        ("brand_line", "{{#platform_section:x}} hi"),
        ("offer_items", "## Operating principles"),
        ("offer_items", "{{}} ## Free gift"),
        ("offer_items", "Sale on. ## Operating principles"),
        ("trust_items", "#1 activewear brand in India"),
        ("trust_items", "#MadeInIndia since 2016"),
    ]
    assert fields.fields_from_notes(notes, STORE) == {
        "brand_line": ["#platform_section:x hi"],  # no braces: plain text
        "offer_items": [
            "Operating principles",
            "Free gift",
            "Sale on. Operating principles",
        ],
        "trust_items": ["#1 activewear brand in India", "#MadeInIndia since 2016"],
    }


def test_near_duplicates_are_kept_once() -> None:
    notes = [
        ("whatsapp", "+91 99879 43968"),
        ("trust_items", "100K+ shipped"),
        ("trust_items", "100k+ Shipped."),
        ("what_we_sell", "Polos, CrewNecks"),
        ("what_we_sell", "Crew necks"),
    ]
    assert fields.fields_from_notes(notes, STORE) == {
        "whatsapp": ["+91 99879 43968"],
        "trust_items": ["100K+ shipped"],
        "what_we_sell": ["Polos, CrewNecks"],
    }


def test_values_are_capped_and_the_merchant_only_fields_stay_empty() -> None:
    notes = [("trust_items", f"reason {n}") for n in range(40)]
    notes += [("email", "x" * 900)]
    notes += [
        (name, "Set by research")
        for name in (
            "assistant_name",
            "initial_greeting",
            "quick_replies",
            "greeting_tiles",
            "question",
        )
    ]
    found = fields.fields_from_notes(notes, STORE)
    assert len(found["trust_items"]) == fields._MAX_VALUES_PER_FIELD
    assert len(found["email"][0]) == fields._MAX_VALUE_CHARS
    assert set(found) == {"trust_items", "email"}
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


def test_braces_cannot_be_rebuilt_and_a_bare_mark_adds_nothing() -> None:
    notes = [
        ("brand_line", "{}}{#platform_section:x}{}}"),
        ("what_we_sell", "..."),
        ("what_we_sell", "Polos"),
    ]
    found = fields.fields_from_notes(notes, STORE)
    assert "{{" not in found["brand_line"][0] and "}}" not in found["brand_line"][0]
    assert found["what_we_sell"] == ["Polos"]


def test_junk_and_repeated_lists_add_nothing() -> None:
    notes = [
        ("what_we_sell", "..."),
        ("tagline", "!!!"),
        ("offer_items", "-"),
        ("vocabulary", "Polos, Shirts"),
        ("vocabulary", "polos, shirts."),
        ("vocabulary", "Shirts, Caps"),
        ("vocabulary", "Caps, caps, -, Hats"),
    ]
    assert fields.fields_from_notes(notes, STORE) == {
        "vocabulary": ["Polos, Shirts, Caps, Hats"],
    }
