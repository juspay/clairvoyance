"""Chat feedback config: template parsing, the widget surface block, and the
``feedback`` column decode (migration 086).

Both feedback kinds are opt-in: a template that says nothing ships both off,
and a disabled end card never reaches the embed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.template.types import (
    ChatFeedbackConfig,
    FeedbackConfig,
    FeedbackPill,
)
from app.api.routers.breeze_buddy.widget.handlers import (
    _extract_widget_config,
    _feedback_wire,
    _surface_wire,
)
from app.database.decoder.breeze_buddy.chat_session import (
    decode_chat_message,
    decode_chat_session,
)
from app.schemas.breeze_buddy.chat import ChatMessage, ChatSession


def _configurations(feedback):
    return SimpleNamespace(feedback=feedback)


# -- template config ----------------------------------------------------------


def test_defaults_are_off_with_four_pills():
    cfg = FeedbackConfig()
    assert cfg.message_feedback.enabled is False
    assert cfg.chat_feedback.enabled is False
    assert [p.id for p in cfg.chat_feedback.pills] == [
        "helped",
        "helped_pay",
        "not_answered",
        "wrong_info",
    ]
    assert cfg.chat_feedback.end_on.idle_seconds is None


def test_duplicate_pill_ids_are_refused():
    with pytest.raises(ValidationError):
        ChatFeedbackConfig(
            pills=[
                FeedbackPill(id="helped", label="Yes"),
                FeedbackPill(id="helped", label="Yes again"),
            ]
        )


@pytest.mark.parametrize("count", [1, 7])
def test_pill_count_is_two_to_six(count):
    with pytest.raises(ValidationError):
        ChatFeedbackConfig(
            pills=[FeedbackPill(id=f"p{i}", label=f"P{i}") for i in range(count)]
        )


@pytest.mark.parametrize("pill_id", ["Helped", "helped pay", "x" * 41, ""])
def test_pill_id_format(pill_id):
    with pytest.raises(ValidationError):
        FeedbackPill(id=pill_id, label="Label")


@pytest.mark.parametrize("seconds", [5, 3601])
def test_idle_seconds_bounds(seconds):
    with pytest.raises(ValidationError):
        ChatFeedbackConfig.model_validate({"end_on": {"idle_seconds": seconds}})


# -- widget surface -----------------------------------------------------------


def test_unset_feedback_ships_both_off():
    wire = _feedback_wire(_configurations(None))
    assert wire.message_feedback is False
    assert wire.chat_feedback is None


def test_disabled_end_card_never_ships():
    cfg = FeedbackConfig.model_validate({"message_feedback": {"enabled": True}})
    wire = _feedback_wire(_configurations(cfg))
    assert wire.message_feedback is True
    assert wire.chat_feedback is None


def test_enabled_end_card_ships_pills_and_end_rules():
    cfg = FeedbackConfig.model_validate(
        {
            "chat_feedback": {
                "enabled": True,
                "pills": [
                    {"id": "helped", "label": "Yes, it helped"},
                    {"id": "helped_pay", "label": "Helped me pay"},
                ],
                "end_on": {
                    "context": {
                        "path": "paymentStatusResponse.orderStatus",
                        "equals": "CHARGED",
                    },
                    "idle_seconds": 120,
                },
            }
        }
    )
    card = _feedback_wire(_configurations(cfg)).chat_feedback
    assert card is not None
    assert [(p.id, p.label) for p in card.pills] == [
        ("helped", "Yes, it helped"),
        ("helped_pay", "Helped me pay"),
    ]
    assert card.end_on.context_path == "paymentStatusResponse.orderStatus"
    assert card.end_on.context_equals == "CHARGED"
    assert card.end_on.idle_seconds == 120


async def test_surface_block_carries_feedback():
    cfg = FeedbackConfig.model_validate({"message_feedback": {"enabled": True}})
    template = SimpleNamespace(
        configurations=SimpleNamespace(feedback=cfg), supported_channels=["chat"]
    )
    surface = _extract_widget_config(template)
    block = await _surface_wire(surface, template, catalog_active="v1", ui_flavors=[])
    assert block.feedback.message_feedback is True
    assert block.feedback.chat_feedback is None


# -- decode -------------------------------------------------------------------


def _msg(row: dict) -> ChatMessage:
    decoded = decode_chat_message(cast(Any, row))
    assert decoded is not None
    return decoded


def _session(row: dict) -> ChatSession:
    decoded = decode_chat_session(cast(Any, row))
    assert decoded is not None
    return decoded


def _message_row(**extra):
    row = {
        "session_id": "s1",
        "idx": 3,
        "role": "assistant",
        "content": "hi",
        "content_blocks": None,
        "ui_blocks": None,
        "created_at": None,
    }
    row.update(extra)
    return row


def test_message_feedback_decodes_from_json_text_and_dict():
    rating = {"rating": "down", "at": "2026-10-27T10:00:00Z"}
    assert _msg(_message_row(feedback=json.dumps(rating))).feedback == rating
    assert _msg(_message_row(feedback=rating)).feedback == rating


def test_message_feedback_is_none_when_unrated_or_not_selected():
    assert _msg(_message_row(feedback=None)).feedback is None
    # A SELECT that doesn't list the column must still decode.
    assert _msg(_message_row()).feedback is None


def test_session_feedback_decodes():
    answer = {"pills": ["helped_pay"], "comment": None, "trigger": "payment_success"}
    row = {
        "id": "s1",
        "template_id": "t1",
        "reseller_id": "r1",
        "status": "ENDED",
        "metadata": None,
        "created_at": None,
        "last_activity_at": None,
        "feedback": json.dumps(answer),
    }
    assert _session(row).feedback == answer
