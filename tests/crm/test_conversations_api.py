"""The Inbox's routes: every one declares the tenancy door and translates
through one route class; a read-only session acts on nothing; the wiring
(consumers, the sweep loop, the router) is in place."""

from types import SimpleNamespace
from typing import Any, List

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.crm.auth import MERCHANT_SCOPE_MARK
from app.crm.conversations import api as conversations_api, contracts, threads
from app.crm.conversations.access import Actor
from app.crm.conversations.errors import (
    ConversationError,
    NotAllowed,
    ThreadConflict,
    ThreadNotFound,
)

THREAD = "5f0c2a8e-3b1d-4c7e-9a6f-2d8e4b1c0a93"


def _routes() -> List[APIRoute]:
    return [r for r in conversations_api.router.routes if isinstance(r, APIRoute)]


def test_every_inbox_route_declares_the_tenancy_door() -> None:
    routes = _routes()
    assert len(routes) >= 11, "the router lost routes — the walk found too few"
    missing = [
        r.path
        for r in routes
        if not any(
            getattr(d.call, MERCHANT_SCOPE_MARK, False)
            for d in r.dependant.dependencies
        )
    ]
    assert missing == [], f"routes without merchant_scope: {missing}"
    assert all(isinstance(r, conversations_api.TranslatingRoute) for r in routes)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ThreadNotFound("no such conversation"), 404),
        (NotAllowed("read-only"), 403),
        (ThreadConflict("someone else took it"), 409),
        (ConversationError("bad address"), 400),
        (RuntimeError("not ours"), None),
    ],
)
def test_each_refusal_earns_one_code(error, code) -> None:
    translated = conversations_api.translate(error)
    assert (translated.status_code if translated else None) == code


def _client(user_id: str, role: str = "merchant") -> TestClient:
    app = FastAPI()
    app.include_router(conversations_api.router, prefix="/conversations")
    app.dependency_overrides[get_current_user_with_rbac] = lambda: SimpleNamespace(
        id=user_id, role=role, username="u", merchant_ids=["shop"], reseller_ids=[]
    )
    return TestClient(app)


def test_a_login_link_session_cannot_take_a_thread(monkeypatch) -> None:
    async def thread(*args: Any):
        raise AssertionError("a read-only session must be refused before any read")

    monkeypatch.setattr(threads, "thread_or_404", thread)
    response = _client("merchant:shop").post(
        f"/conversations/{THREAD}/claim", json={"merchant_id": "shop"}
    )
    assert response.status_code == 403 and "read-only" in response.text


def test_another_merchants_thread_is_refused_at_the_door(monkeypatch) -> None:
    response = _client("u-1").get(
        f"/conversations/{THREAD}", params={"merchant_id": "other"}
    )
    assert response.status_code == 403


def test_only_managers_assign(monkeypatch) -> None:
    async def thread(*args: Any):
        raise AssertionError("refused before any read")

    monkeypatch.setattr(threads, "thread_or_404", thread)
    response = _client("u-2", role="user").post(
        f"/conversations/{THREAD}/assign",
        json={"merchant_id": "shop", "user_id": "u-9"},
    )
    assert response.status_code == 403


def test_a_reply_is_text_or_a_template_never_both() -> None:
    response = _client("u-1").post(
        f"/conversations/{THREAD}/reply",
        json={"merchant_id": "shop", "text": "hi", "template_id": "cod"},
    )
    assert response.status_code == 422


async def test_human_handoff_off_refuses_a_take_over(monkeypatch) -> None:
    async def thread(merchant_id: str, thread_id: str):
        return SimpleNamespace(
            id=thread_id, channel="whatsapp", merchant_id=merchant_id
        )

    async def settings(thread):
        return SimpleNamespace(human_handoff=False)

    monkeypatch.setattr(threads, "thread_or_404", thread)
    monkeypatch.setattr(threads, "settings_for", settings)
    with pytest.raises(NotAllowed, match="off"):
        await threads.take_over(
            "shop", THREAD, Actor("u-1", read_only=False, manager=False)
        )


# --- the wiring --------------------------------------------------------------


def test_the_consumers_and_the_sweep_loop_are_wired() -> None:
    import app.crm.worker_main as worker_main  # noqa: F401 — registration runs at import
    from app.crm.record.consumers import consumers

    registered = consumers()
    assert contracts.consume_conversation_event in registered
    assert contracts.consume_buddy_moved in registered
    assert "walker" in worker_main.ROLES


def test_the_inbox_is_mounted_at_conversations() -> None:
    from app.crm.api import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/conversations" in paths and "/conversations/stream" in paths


async def test_the_sweep_tick_runs_once_per_interval(monkeypatch) -> None:
    from app.crm.conversations import workers

    monkeypatch.setattr(workers, "_last_tick", 0.0)
    assert await workers.claim_inbox_tick(1) == [workers.TICK]
    assert await workers.claim_inbox_tick(1) == []
