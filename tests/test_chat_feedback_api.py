"""The two widget feedback routes: rate a bot reply, and answer the end card.

Both fail closed when the template has the feedback off, both work on an
ENDED session (the end card is shown after the chat ends), and the end card
only accepts the pill ids its template configured.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.ai.voice.agents.breeze_buddy.template.types import FeedbackConfig
from app.api.routers.breeze_buddy.widget import handlers, router
from app.api.security.breeze_buddy.widget_token import (
    WidgetSessionContext,
    require_widget_session,
)
from app.database.queries.breeze_buddy.chat_analytics import (
    get_chat_analytics_summary_query,
)
from app.database.queries.breeze_buddy.chat_session import (
    set_chat_message_feedback_query,
    set_chat_session_feedback_query,
)
from app.schemas.breeze_buddy.chat import (
    ChatFeedbackRequest,
    ChatMessage,
    ChatMessageRole,
    ChatSession,
    ChatSessionStatus,
    MessageFeedbackRequest,
)

CTX = WidgetSessionContext(session_id="s1", widget_config_id="w1", subject="anon")


def _feedback_cfg(**overrides: Any) -> FeedbackConfig:
    return FeedbackConfig.model_validate(
        {
            "message_feedback": {"enabled": True},
            "chat_feedback": {"enabled": True},
            **overrides,
        }
    )


@pytest.fixture()
def wired(monkeypatch):
    """Stub the gate's reads; record what the writes received."""
    calls: Dict[str, List[Any]] = {"message": [], "session": []}
    state: Dict[str, Any] = {
        "feedback": _feedback_cfg(),
        "status": ChatSessionStatus.ACTIVE,
        "message_row": True,
    }

    async def config(**_):
        return SimpleNamespace(id="w1", max_messages_per_ip_hour=100)

    async def ip_limit(**_):
        return None

    async def session(session_id: str):
        return ChatSession(
            id=session_id, template_id="t1", reseller_id="r1", status=state["status"]
        )

    async def template(_template_id: str):
        return SimpleNamespace(
            configurations=SimpleNamespace(feedback=state["feedback"])
        )

    async def set_message(session_id: str, idx: int, feedback: Optional[dict]):
        calls["message"].append((session_id, idx, feedback))
        if not state["message_row"]:
            return None
        return ChatMessage(
            session_id=session_id,
            idx=idx,
            role=ChatMessageRole.ASSISTANT,
            feedback=feedback,
        )

    async def set_session(session_id: str, feedback: dict):
        calls["session"].append((session_id, feedback))
        return ChatSession(
            id=session_id, template_id="t1", reseller_id="r1", feedback=feedback
        )

    monkeypatch.setattr(handlers, "resolve_session_widget_config", config)
    monkeypatch.setattr(handlers, "enforce_widget_ip_limit", ip_limit)
    monkeypatch.setattr(handlers, "get_chat_session_by_id", session)
    monkeypatch.setattr(handlers, "assert_widget_session_ownership", lambda *_: None)
    monkeypatch.setattr(handlers, "get_template_by_id_cached", template)
    monkeypatch.setattr(handlers, "set_chat_message_feedback", set_message)
    monkeypatch.setattr(handlers, "set_chat_session_feedback", set_session)
    return SimpleNamespace(calls=calls, state=state)


async def _rate(rating: Optional[str], idx: int = 3):
    return await handlers.set_message_feedback_handler(
        "s1", idx, MessageFeedbackRequest(rating=rating), request=None, ctx=CTX  # type: ignore[arg-type]
    )


async def _answer(**body: Any):
    return await handlers.set_chat_feedback_handler(
        "s1", ChatFeedbackRequest(**body), request=None, ctx=CTX  # type: ignore[arg-type]
    )


# -- rate a reply -------------------------------------------------------------


async def test_rating_is_stored_with_a_timestamp(wired):
    res = await _rate("down")
    ((_, idx, stored),) = wired.calls["message"]
    assert idx == 3
    assert stored["rating"] == "down" and stored["at"]
    assert res.feedback == stored


async def test_null_rating_clears_it(wired):
    await _rate(None)
    assert wired.calls["message"] == [("s1", 3, None)]


async def test_rating_works_after_the_chat_ended(wired):
    wired.state["status"] = ChatSessionStatus.ENDED
    feedback = (await _rate("up")).feedback
    assert feedback is not None and feedback["rating"] == "up"


async def test_rating_refused_when_message_feedback_is_off(wired):
    wired.state["feedback"] = _feedback_cfg(message_feedback={"enabled": False})
    with pytest.raises(HTTPException) as raised:
        await _rate("up")
    assert raised.value.status_code == 403
    assert wired.calls["message"] == []


async def test_rating_refused_when_template_has_no_feedback(wired):
    wired.state["feedback"] = None
    with pytest.raises(HTTPException) as raised:
        await _rate("up")
    assert raised.value.status_code == 403


async def test_rating_a_non_bot_reply_is_404(wired):
    wired.state["message_row"] = False
    with pytest.raises(HTTPException) as raised:
        await _rate("up", idx=0)
    assert raised.value.status_code == 404


# -- end card -----------------------------------------------------------------


async def test_card_answer_is_stored(wired):
    res = await _answer(
        pills=["helped_pay", "helped", "helped_pay"],
        comment="  clear fee table  ",
        trigger="context",
    )
    ((_, stored),) = wired.calls["session"]
    assert stored["pills"] == ["helped_pay", "helped"]  # de-duped, order kept
    assert stored["comment"] == "clear fee table"
    assert stored["trigger"] == "context"
    assert stored["at"]
    assert res.feedback == stored


async def test_unknown_pill_is_422(wired):
    with pytest.raises(HTTPException) as raised:
        await _answer(pills=["made_up"])
    assert raised.value.status_code == 422
    assert wired.calls["session"] == []


async def test_empty_answer_is_422(wired):
    with pytest.raises(HTTPException) as raised:
        await _answer(pills=[], comment="   ")
    assert raised.value.status_code == 422


async def test_card_refused_when_chat_feedback_is_off(wired):
    wired.state["feedback"] = _feedback_cfg(chat_feedback={"enabled": False})
    with pytest.raises(HTTPException) as raised:
        await _answer(pills=["helped"])
    assert raised.value.status_code == 403


# -- SQL, routes, analytics ---------------------------------------------------


def test_message_update_only_touches_bot_replies():
    query, values = set_chat_message_feedback_query("s1", 4, '{"rating": "up"}')
    assert "role = 'assistant'" in query
    assert "WHERE session_id = $1 AND idx = $2" in query
    assert values == ["s1", 4, '{"rating": "up"}']


def test_session_update_is_by_id():
    query, values = set_chat_session_feedback_query("s1", "{}")
    assert "WHERE id = $1" in query and "feedback = $2::jsonb" in query
    assert values == ["s1", "{}"]


def test_routes_are_mounted_with_preflight():
    methods: Dict[str, set] = {}
    for route in router.routes:
        methods.setdefault(getattr(route, "path", ""), set()).update(
            getattr(route, "methods", set()) or set()
        )
    for path in (
        "/widget/session/{session_id}/messages/{idx}/feedback",
        "/widget/session/{session_id}/feedback",
    ):
        match = [p for p in methods if p.endswith(path)]
        assert match, f"{path} not mounted"
        assert {"POST", "OPTIONS"} <= methods[match[0]]


def test_out_of_range_idx_is_422_before_the_db():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_widget_session] = lambda: None
    client = TestClient(app)
    for idx in (-1, 2**31):  # chat_message.idx is int4
        resp = client.post(
            f"/widget/session/s1/messages/{idx}/feedback", json={"rating": "up"}
        )
        assert resp.status_code == 422, idx


def test_summary_exposes_feedback_counts():
    query, _ = get_chat_analytics_summary_query({})
    for alias in ("AS thumbs_up", "AS thumbs_down", "AS chat_feedback_count"):
        assert alias in query
