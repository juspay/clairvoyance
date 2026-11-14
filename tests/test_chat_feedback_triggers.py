"""End-of-chat card event rules: the server checks each /context push and
tells the widget to show the card (the widget never sees the rules)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest
from fastapi import Request

from app.ai.voice.agents.breeze_buddy.chat.feedback_triggers import (
    matching_feedback_event,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    FeedbackConfig,
    FeedbackEvent,
)
from app.api.routers.breeze_buddy.widget import handlers
from app.api.security.breeze_buddy.widget_token import WidgetSessionContext
from app.schemas.breeze_buddy.chat import (
    ChatSession,
    ChatSessionStatus,
    UpdateWidgetContextRequest,
)

PAID = FeedbackEvent(
    field="paymentStatusResponse.orderStatus", values=["CHARGED", "SUCCESS"]
)
BOOKED = FeedbackEvent(field="booking.status", values=["confirmed"])


# -- the matcher --------------------------------------------------------------


def test_matches_any_value_ignoring_text_case():
    for status in ("CHARGED", "success", "Charged"):
        data = {"paymentStatusResponse": {"orderStatus": status}}
        assert matching_feedback_event([PAID], data) is PAID


def test_matches_any_rule_in_facts_or_state():
    assert (
        matching_feedback_event(
            [PAID, BOOKED], None, {"booking": {"status": "CONFIRMED"}}
        )
        is BOOKED
    )


def test_no_match_on_another_value_or_a_missing_path():
    assert (
        matching_feedback_event(
            [PAID], {"paymentStatusResponse": {"orderStatus": "PENDING_VBV"}}
        )
        is None
    )
    assert matching_feedback_event([PAID], {"paymentStatusResponse": "CHARGED"}) is None
    assert matching_feedback_event([PAID], {}, None) is None
    assert (
        matching_feedback_event(
            [], {"paymentStatusResponse": {"orderStatus": "CHARGED"}}
        )
        is None
    )


@pytest.mark.parametrize(
    "found, want, hit",
    [
        (True, True, True),
        ("true", True, False),
        (1, True, False),
        (True, 1, False),
        (99, 99.0, True),
        ("99", 99, False),
    ],
)
def test_numbers_and_booleans_match_exactly(found, want, hit):
    rule = FeedbackEvent(field="paid", values=[want])
    assert (matching_feedback_event([rule], {"paid": found}) is rule) is hit


# -- the /context handler -----------------------------------------------------


def _feedback(**overrides: Any) -> FeedbackConfig:
    return FeedbackConfig.model_validate(
        {"rate_chat": True, "events": [PAID.model_dump()], **overrides}
    )


@pytest.fixture()
def wired(monkeypatch):
    state: Dict[str, Any] = {"feedback": _feedback(), "answer": None}

    async def config(**_):
        return SimpleNamespace(id="w1", max_messages_per_ip_hour=100)

    async def ip_limit(**_):
        return None

    async def session(session_id: str):
        return ChatSession(
            id=session_id,
            template_id="t1",
            reseller_id="r1",
            status=ChatSessionStatus.ACTIVE,
            feedback=state["answer"],
        )

    async def template(_template_id: str):
        return SimpleNamespace(
            configurations=SimpleNamespace(
                client_context=None, feedback=state["feedback"]
            )
        )

    async def agent_state(_session_id: str):
        return None

    monkeypatch.setattr(handlers, "resolve_session_widget_config", config)
    monkeypatch.setattr(handlers, "enforce_widget_ip_limit", ip_limit)
    monkeypatch.setattr(handlers, "get_chat_session_by_id", session)
    monkeypatch.setattr(handlers, "assert_widget_session_ownership", lambda *_: None)
    monkeypatch.setattr(handlers, "get_template_by_id_cached", template)
    monkeypatch.setattr(handlers, "get_agent_session_state", agent_state)
    return state


async def _push(facts: Optional[dict] = None, state: Optional[dict] = None):
    return await handlers.update_widget_context_handler(
        "s1",
        UpdateWidgetContextRequest(facts=facts, state=state),
        Request({"type": "http", "headers": [], "client": ("1.2.3.4", 0)}),
        WidgetSessionContext(session_id="s1", widget_config_id="w1", subject="anon"),
    )


async def test_push_that_matches_shows_the_card(wired):
    res = await _push(facts={"paymentStatusResponse": {"orderStatus": "charged"}})
    assert res.show_feedback_card is True


async def test_push_that_does_not_match_does_not(wired):
    res = await _push(facts={"paymentStatusResponse": {"orderStatus": "PENDING"}})
    assert res.show_feedback_card is False


async def test_card_off_or_already_answered_never_shows(wired):
    paid = {"paymentStatusResponse": {"orderStatus": "CHARGED"}}
    wired["answer"] = {"skipped": True, "trigger": "new_chat"}
    assert (await _push(facts=paid)).show_feedback_card is False
    wired["answer"] = None
    wired["feedback"] = _feedback(rate_chat=False)
    assert (await _push(facts=paid)).show_feedback_card is False
    wired["feedback"] = None
    assert (await _push(facts=paid)).show_feedback_card is False
