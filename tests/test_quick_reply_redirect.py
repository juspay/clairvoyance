"""Tests for redirect (``open_url``) support on quick-reply pills.

Quick replies historically only sent ``value`` back to the agent. A pill
can now optionally carry an ``action`` — an ``open_url`` action makes it
redirect instead of messaging. These tests pin both the new behavior and
backward compatibility (no ``action`` => unchanged send-to-agent path).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.template.types import QuickReplyOption

# Load the template package (ui_catalog) before any chat/* module — same
# circular-import precaution as the sibling ui_prompt / ui_stream tests.
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    Icon,
    OpenUrlAction,
    QuickReplies,
    QuickReplyItem,
    ToAssistantAction,
    validate_props,
)
from app.api.routers.breeze_buddy.widget.handlers import _extract_widget_config
from app.schemas.breeze_buddy.chat import QuickReplyWire

# ---------------------------------------------------------------------------
# OpenUrlAction.target
# ---------------------------------------------------------------------------


def test_open_url_defaults_to_new_tab():
    """Existing open_url actions (no target) keep opening in a new tab."""
    action = OpenUrlAction(url="https://shop.example/orders/1")
    assert action.target == "new_tab"


def test_open_url_accepts_same_tab():
    action = OpenUrlAction(url="https://shop.example/checkout", target="same_tab")
    assert action.target == "same_tab"


def test_open_url_rejects_unknown_target():
    with pytest.raises(ValidationError):
        # model_validate (vs the constructor) so the intentionally-bad literal
        # is rejected at runtime without a static argument-type error.
        OpenUrlAction.model_validate(
            {"url": "https://shop.example/x", "target": "popup"}
        )


# ---------------------------------------------------------------------------
# Dynamic QuickReplies primitive
# ---------------------------------------------------------------------------


def test_quick_replies_with_redirect_action_validates():
    qr = validate_props(
        "QuickReplies",
        {
            "items": [
                {"label": "Yes"},
                {
                    "label": "View order",
                    "action": {
                        "type": "open_url",
                        "url": "https://shop.example/orders/1",
                        "target": "same_tab",
                    },
                },
            ]
        },
    )
    assert isinstance(qr, QuickReplies)
    assert qr.items[0].action is None  # backward-compat: message pill
    redirect = qr.items[1].action
    assert isinstance(redirect, OpenUrlAction)
    assert redirect.type == "open_url"
    assert redirect.target == "same_tab"


def test_quick_reply_item_without_action_is_backward_compatible():
    item = QuickReplyItem(label="Track", value="Track my order")
    assert item.action is None
    assert item.value == "Track my order"


def test_quick_reply_item_accepts_to_assistant_action():
    item = QuickReplyItem(label="x", action={"type": "to_assistant", "msg": "hi"})
    assert isinstance(item.action, ToAssistantAction)
    assert item.action.type == "to_assistant"


# ---------------------------------------------------------------------------
# Static config model (QuickReplyOption) + wire (QuickReplyWire)
# ---------------------------------------------------------------------------


def test_static_option_redirect_needs_no_value():
    opt = QuickReplyOption(
        label="Checkout",
        action={"type": "open_url", "url": "https://shop.example/checkout"},
    )
    assert opt.value is None
    assert isinstance(opt.action, OpenUrlAction)
    assert opt.action.type == "open_url"


def test_static_option_without_action_is_backward_compatible():
    opt = QuickReplyOption(label="Track", value="Track my order")
    assert opt.action is None


def test_wire_carries_action_with_null_value():
    wire = QuickReplyWire(
        label="Checkout",
        value=None,
        action={"type": "open_url", "url": "https://shop.example/checkout"},
    )
    assert wire.value is None
    assert isinstance(wire.action, OpenUrlAction)
    assert wire.action.target == "new_tab"


# ---------------------------------------------------------------------------
# Widget handler — value fallback only for message pills
# ---------------------------------------------------------------------------


def _template_with_quick_replies(options):
    configurations = SimpleNamespace(quick_replies=options, enable_text_input=True)
    return SimpleNamespace(configurations=configurations)


def test_handler_message_pill_falls_back_to_label():
    template = _template_with_quick_replies([QuickReplyOption(label="Track my order")])
    replies, enable_text_input = _extract_widget_config(template)
    assert enable_text_input is True
    assert replies[0].value == "Track my order"  # label fallback
    assert replies[0].action is None


def test_handler_redirect_pill_has_null_value_and_passes_action():
    template = _template_with_quick_replies(
        [
            QuickReplyOption(
                label="View your order",
                # Even with a value set, a redirect pill must drop it: it
                # navigates, it never messages the agent.
                value="should be dropped",
                action={"type": "open_url", "url": "https://shop.example/orders/1"},
            )
        ]
    )
    replies, _ = _extract_widget_config(template)
    # Redirect pills don't message the agent — value is always None.
    assert replies[0].value is None
    assert isinstance(replies[0].action, OpenUrlAction)
    assert replies[0].action.type == "open_url"


def test_handler_no_configurations_returns_defaults():
    assert _extract_widget_config(SimpleNamespace(configurations=None)) == ([], True)


# ---------------------------------------------------------------------------
# Icon — backend-driven chicklet icon (static chicklets only)
# ---------------------------------------------------------------------------


def test_icon_validates_with_url_and_alt():
    icon = Icon(url="https://cdn.example/user.svg", alt="My account")
    assert str(icon.url) == "https://cdn.example/user.svg"
    assert icon.alt == "My account"


def test_icon_alt_is_optional():
    icon = Icon(url="https://cdn.example/cart.svg")
    assert icon.alt is None


def test_icon_rejects_unknown_key():
    with pytest.raises(ValidationError):
        Icon.model_validate({"url": "https://cdn.example/x.svg", "size": "lg"})


def test_static_option_carries_icon():
    opt = QuickReplyOption(
        label="My account",
        action={"type": "open_url", "url": "https://shop.example/account"},
        icon={"url": "https://cdn.example/user.svg", "alt": "My account"},
    )
    assert isinstance(opt.icon, Icon)
    assert opt.icon.alt == "My account"


def test_static_option_without_icon_is_backward_compatible():
    opt = QuickReplyOption(label="Track", value="Track my order")
    assert opt.icon is None


def test_handler_passes_icon_through_for_message_pill():
    template = _template_with_quick_replies(
        [
            QuickReplyOption(
                label="What's on sale?",
                value="Show me what's on sale",
                icon={"url": "https://cdn.example/sale.svg", "alt": "Sale"},
            )
        ]
    )
    replies, _ = _extract_widget_config(template)
    # Icon is presentational — it survives regardless of value/action.
    assert replies[0].value == "Show me what's on sale"
    assert isinstance(replies[0].icon, Icon)
    assert str(replies[0].icon.url) == "https://cdn.example/sale.svg"


def test_handler_passes_icon_through_for_redirect_pill():
    template = _template_with_quick_replies(
        [
            QuickReplyOption(
                label="My account",
                action={"type": "open_url", "url": "https://shop.example/account"},
                icon={"url": "https://cdn.example/user.svg", "alt": "My account"},
            )
        ]
    )
    replies, _ = _extract_widget_config(template)
    assert replies[0].value is None  # redirect pill
    assert isinstance(replies[0].icon, Icon)
    assert replies[0].icon.alt == "My account"


def test_handler_pill_without_icon_emits_none():
    template = _template_with_quick_replies([QuickReplyOption(label="Track my order")])
    replies, _ = _extract_widget_config(template)
    assert replies[0].icon is None
