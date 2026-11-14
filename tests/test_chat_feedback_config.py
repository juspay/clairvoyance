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


def test_defaults_are_off_with_generic_pills():
    cfg = FeedbackConfig()
    assert cfg.rate_replies is False
    assert cfg.rate_chat is False
    assert [p.id for p in cfg.pills] == ["helped", "not_answered", "wrong_info"]
    assert cfg.events == []


def test_rate_chat_needs_no_event_rule():
    # New chat and the server ending the chat are built in, so events are
    # optional.
    assert FeedbackConfig.model_validate({"rate_chat": True}).rate_chat is True


def test_duplicate_pill_ids_are_refused():
    with pytest.raises(ValidationError):
        FeedbackConfig(
            pills=[
                FeedbackPill(id="helped", label="Yes"),
                FeedbackPill(id="helped", label="Yes again"),
            ]
        )


@pytest.mark.parametrize("count", [1, 7])
def test_pill_count_is_two_to_six(count):
    with pytest.raises(ValidationError):
        FeedbackConfig(
            pills=[FeedbackPill(id=f"p{i}", label=f"P{i}") for i in range(count)]
        )


@pytest.mark.parametrize("pill_id", ["Helped", "helped pay", "x" * 41, ""])
def test_pill_id_format(pill_id):
    with pytest.raises(ValidationError):
        FeedbackPill(id=pill_id, label="Label")


def test_events_parse():
    cfg = FeedbackConfig.model_validate(
        {
            "rate_chat": True,
            "events": [
                {
                    "field": "paymentStatusResponse.orderStatus",
                    "values": ["CHARGED", "SUCCESS"],
                },
                {"field": "booking.status", "values": ["confirmed"]},
            ],
        }
    )
    assert [e.field for e in cfg.events] == [
        "paymentStatusResponse.orderStatus",
        "booking.status",
    ]


def test_at_most_five_events():
    rule = {"field": "a.b", "values": ["x"]}
    FeedbackConfig.model_validate({"events": [rule] * 5})
    with pytest.raises(ValidationError):
        FeedbackConfig.model_validate({"events": [rule] * 6})


@pytest.mark.parametrize(
    "value, kind", [(True, bool), (99, int), (99.5, float), ("CHARGED", str)]
)
def test_event_values_keep_their_type(value, kind):
    cfg = FeedbackConfig.model_validate(
        {"events": [{"field": "a.b", "values": [value]}]}
    )
    assert type(cfg.events[0].values[0]) is kind
    assert cfg.events[0].values == [value]


@pytest.mark.parametrize("values", [[], ["v"] * 11, "CHARGED"])
def test_event_values_are_a_list_of_one_to_ten(values):
    with pytest.raises(ValidationError):
        FeedbackConfig.model_validate({"events": [{"field": "a.b", "values": values}]})


@pytest.mark.parametrize(
    "field",
    ["", " ", ".", "a..b", ".a", "a.", "paymentStatusResponse.orderStatus ", "a. b"],
)
def test_event_field_must_be_a_clean_dot_path(field):
    with pytest.raises(ValidationError):
        FeedbackConfig.model_validate({"events": [{"field": field, "values": ["x"]}]})


@pytest.mark.parametrize("field", ["paid", "booking.status", "a.b-c.d_1"])
def test_event_field_accepts_dot_paths(field):
    FeedbackConfig.model_validate({"events": [{"field": field, "values": ["x"]}]})


@pytest.mark.parametrize("blank", ["", "   "])
def test_event_values_cannot_be_blank_text(blank):
    with pytest.raises(ValidationError):
        FeedbackConfig.model_validate(
            {"events": [{"field": "a", "values": ["CHARGED", blank]}]}
        )


# -- widget surface -----------------------------------------------------------


def test_unset_feedback_ships_both_off():
    wire = _feedback_wire(_configurations(None))
    assert wire.rate_replies is False
    assert wire.rate_chat is None


def test_rate_chat_off_never_ships():
    cfg = FeedbackConfig.model_validate({"rate_replies": True})
    wire = _feedback_wire(_configurations(cfg))
    assert wire.rate_replies is True
    assert wire.rate_chat is None


def test_rate_chat_ships_pills_only():
    cfg = FeedbackConfig.model_validate(
        {
            "rate_chat": True,
            "pills": [
                {"id": "helped", "label": "Yes, it helped"},
                {"id": "helped_pay", "label": "Helped me pay"},
            ],
            "events": [
                {
                    "field": "paymentStatusResponse.orderStatus",
                    "values": ["CHARGED", "SUCCESS"],
                }
            ],
        }
    )
    card = _feedback_wire(_configurations(cfg)).rate_chat
    assert card is not None
    # Event rules stay on the server.
    assert card.model_dump() == {
        "pills": [
            {"id": "helped", "label": "Yes, it helped"},
            {"id": "helped_pay", "label": "Helped me pay"},
        ]
    }


async def test_surface_block_carries_feedback():
    cfg = FeedbackConfig.model_validate({"rate_replies": True})
    template = SimpleNamespace(
        configurations=SimpleNamespace(feedback=cfg), supported_channels=["chat"]
    )
    surface = _extract_widget_config(template)
    block = await _surface_wire(surface, template, catalog_active="v1", ui_flavors=[])
    assert block.feedback.rate_replies is True
    assert block.feedback.rate_chat is None


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
    answer = {"pills": ["helped"], "comment": None, "trigger": "event"}
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
