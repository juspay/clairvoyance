"""Quick-reply chips are user-facing copy: a raw identifier in one is always
a model mistake (live-observed "Select <journey uuid>" pills), so the
harvest path drops such labels instead of showing them."""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.chat.agent.runtime import _chip_labels


def test_chip_labels_drop_raw_identifiers():
    raw = [
        "Book now",
        "Select 3f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b",
        {"label": "Ticket 0123456789abcdef01"},
        {"label": "Show other routes"},
        "",
        {"label": "   "},
        42,
    ]
    assert _chip_labels(raw) == ["Book now", "Show other routes"]


def test_chip_labels_keep_ordinary_labels_and_cap_at_five():
    labels = ["Bus 5C", "Platform 2", "Metro", "Train", "Walk", "Auto"]
    assert _chip_labels(labels) == labels[:5]
    assert _chip_labels("not a list") == []


def test_render_ui_quick_replies_extraction_uses_the_same_guard():
    from app.ai.voice.agents.breeze_buddy.chat.ui.chips import carries_identifier

    assert carries_identifier("Select 3f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b")
    assert carries_identifier("order 0123456789ABCDEF")
    # prefixed / embedded ids (no word boundary before the hex run)
    assert carries_identifier("Order_0123456789abcdef01")
    assert carries_identifier("Pay 0xdeadbeefcafebabe now")
    assert not carries_identifier("Deadline 2026-09-07 at 15:30")
    # pure digit runs are numbers people read, not ids
    assert not carries_identifier("Track order 1234567890123456")
    assert not carries_identifier("Card ending 4242")
    assert not carries_identifier("Bus 5C") and not carries_identifier("Platform 2")


def test_render_ui_quick_replies_drop_ids_and_explain_when_none_survive():
    from app.ai.voice.agents.breeze_buddy.chat.ui.binding import BindingStore
    from app.ai.voice.agents.breeze_buddy.chat.ui.render_ui_tool import (
        execute_render_ui,
        render_ui_components,
    )
    from app.ai.voice.agents.breeze_buddy.template.ui_catalog import resolve_allowlist

    allow = resolve_allowlist(enabled_groups=["core", "commerce"])
    comps = render_ui_components(allow, True)

    def run(chips):
        return execute_render_ui(
            {"component": "QuickReplies", "quick_replies": chips},
            store=BindingStore(),
            allowlist=allow,
            components=comps,
            op_id="rui1",
        )

    mixed = run(["Book now", "Select 3f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b", "Help"])
    assert mixed.decision == "rendered"
    assert [i["label"] for i in mixed.ops[0]["props"]["items"]] == ["Book now", "Help"]

    none_left = run(["Select 3f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b"])
    assert none_left.decision != "rendered" and none_left.ops == []
    assert "raw identifier" in none_left.fn_result["error"]

    numeric = run(["Track order 1234567890123456"])
    assert numeric.decision == "rendered"
