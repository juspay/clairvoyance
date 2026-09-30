"""Tests for Buddy Copilot scope injection into normal chat sessions."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.api.routers.breeze_buddy.chat as chat_router
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.api.routers.breeze_buddy.chat import handlers as chat_handlers
from app.api.routers.breeze_buddy.chat.rbac import validate_chat_session_access
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.chat import (
    ChatSession,
    ChatSessionStatus,
    CreateChatSessionRequest,
    SendChatMessageRequest,
)
from app.schemas.breeze_buddy.copilot import CopilotScopeRequest
from app.services.breeze_buddy.copilot import scope as scope_service
from app.services.breeze_buddy.copilot.scope import CopilotScopeError

DATA_TEMPLATE_ID = "11111111-1111-4111-8111-111111111111"


def _user(
    *,
    user_id: str = "user-1",
    role: UserRole = UserRole.MERCHANT,
    reseller_ids: list[str] | None = None,
    merchant_ids: list[str] | None = None,
    permissions: list[str] | None = None,
) -> UserInfo:
    return UserInfo(
        id=user_id,
        username=user_id,
        role=role,
        reseller_ids=reseller_ids or ["dashboard-reseller"],
        merchant_ids=merchant_ids or ["data-merchant"],
        permissions=permissions or ["analytics:own"],
    )


def _dashboard_template() -> TemplateModel:
    return TemplateModel(
        id="dashboard-template",
        reseller_id="dashboard-reseller",
        merchant_id="dashboard-runtime",
        name="Dashboard Assist",
        flow={"nodes": []},
        supported_channels=["chat"],
    )


def _capture_session_persist(monkeypatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def persist_session(*, metadata, **kwargs):
        captured["metadata"] = metadata
        captured["persist_kwargs"] = kwargs
        return ChatSession(
            id="session-1",
            template_id=kwargs["template_id"],
            reseller_id=kwargs["reseller_id"],
            merchant_id=kwargs["merchant_id"],
            metadata=metadata,
        )

    async def render_vars(_template, _persisted):
        return {}

    monkeypatch.setattr(chat_handlers, "create_chat_session", persist_session)
    monkeypatch.setattr(chat_handlers, "build_render_template_vars", render_vars)
    return captured


def test_normal_chat_session_creation_is_unchanged(monkeypatch):
    template = _dashboard_template()
    captured = _capture_session_persist(monkeypatch)

    response = asyncio.run(
        chat_handlers.create_chat_session_handler(
            CreateChatSessionRequest(
                template_id=template.id,
                template_vars={"customer_name": "Asha"},
                metadata={"safe": True},
            ),
            template,
            _user(),
        )
    )

    assert response.session_id == "session-1"
    assert response.status == ChatSessionStatus.ACTIVE
    assert response.greeting is None
    assert captured["metadata"] == {
        "safe": True,
        "template_vars": {"customer_name": "Asha"},
    }
    assert captured["persist_kwargs"] == {
        "template_id": "dashboard-template",
        "reseller_id": "dashboard-reseller",
        "merchant_id": "dashboard-runtime",
    }


def test_copilot_scope_is_resolved_and_persisted_on_normal_chat_session(
    monkeypatch,
):
    template = _dashboard_template()
    captured = _capture_session_persist(monkeypatch)

    response = asyncio.run(
        chat_handlers.create_chat_session_handler(
            CreateChatSessionRequest(
                template_id=template.id,
                metadata={"source": "dashboard"},
                copilot_scope=CopilotScopeRequest(
                    data_merchant_id="data-merchant",
                ),
            ),
            template,
            _user(),
        )
    )

    assert response.session_id == "session-1"
    metadata = cast(dict[str, Any], captured["metadata"])
    copilot_metadata = cast(dict[str, Any], metadata["copilot"])
    copilot_data = cast(dict[str, Any], copilot_metadata["data"])
    copilot_actor = cast(dict[str, Any], copilot_metadata["actor"])
    persist_kwargs = cast(dict[str, Any], captured["persist_kwargs"])

    assert metadata["source"] == "dashboard"
    assert metadata["template_vars"] == {}
    assert copilot_data == {
        "data_merchant_id": "data-merchant",
        "data_template_id": None,
    }
    assert copilot_actor == {"user_id": "user-1"}
    assert "runtime_merchant_id" not in copilot_metadata
    assert "runtime_template_id" not in copilot_metadata
    assert persist_kwargs["merchant_id"] == "dashboard-runtime"


def test_copilot_scope_does_not_require_analytics_permission(monkeypatch):
    template = _dashboard_template()
    captured = _capture_session_persist(monkeypatch)

    asyncio.run(
        chat_handlers.create_chat_session_handler(
            CreateChatSessionRequest(
                template_id=template.id,
                copilot_scope=CopilotScopeRequest(
                    data_merchant_id="data-merchant",
                ),
            ),
            template,
            _user(permissions=["read:own_data"]),
        )
    )

    metadata = cast(dict[str, Any], captured["metadata"])
    copilot_metadata = cast(dict[str, Any], metadata["copilot"])
    copilot_data = cast(dict[str, Any], copilot_metadata["data"])
    assert copilot_data["data_merchant_id"] == "data-merchant"


@pytest.mark.parametrize(
    ("metadata_key", "expected_detail"),
    [
        ("copilot", "metadata.copilot is server-owned"),
        ("template_vars", "metadata.template_vars is server-owned"),
    ],
)
def test_client_cannot_set_server_owned_metadata(
    monkeypatch,
    metadata_key,
    expected_detail,
):
    template = _dashboard_template()
    captured = _capture_session_persist(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            chat_handlers.create_chat_session_handler(
                CreateChatSessionRequest(
                    template_id=template.id,
                    metadata={metadata_key: {"forged": True}},
                    copilot_scope=CopilotScopeRequest(
                        data_merchant_id="data-merchant",
                    ),
                ),
                template,
                _user(),
            )
        )

    assert exc.value.status_code == 422
    assert exc.value.detail == expected_detail
    assert "metadata" not in captured


@pytest.mark.parametrize(
    ("scope_request", "user", "expected_status", "expected_code"),
    [
        (
            CopilotScopeRequest(data_merchant_id="other-merchant"),
            _user(),
            403,
            "unauthorized_merchant",
        ),
        (
            CopilotScopeRequest(),
            _user(role=UserRole.ADMIN, merchant_ids=["*"]),
            400,
            "ambiguous_merchant",
        ),
    ],
)
def test_rejects_invalid_copilot_scope(
    monkeypatch,
    scope_request,
    user,
    expected_status,
    expected_code,
):
    template = _dashboard_template()
    _capture_session_persist(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            chat_handlers.create_chat_session_handler(
                CreateChatSessionRequest(
                    template_id=template.id,
                    copilot_scope=scope_request,
                ),
                template,
                user,
            )
        )

    assert exc.value.status_code == expected_status
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == expected_code


def test_rejects_data_template_from_another_merchant(monkeypatch):
    template = _dashboard_template()
    _capture_session_persist(monkeypatch)

    async def load_template_merchant(_template_id: str):
        return "other-merchant"

    monkeypatch.setattr(
        scope_service,
        "get_template_merchant_id",
        load_template_merchant,
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            chat_handlers.create_chat_session_handler(
                CreateChatSessionRequest(
                    template_id=template.id,
                    copilot_scope=CopilotScopeRequest(
                        data_merchant_id="data-merchant",
                        data_template_id=DATA_TEMPLATE_ID,
                    ),
                ),
                template,
                _user(),
            )
        )

    assert exc.value.status_code == 403
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "unauthorized_template"


def test_copilot_scope_request_cannot_override_assist_template():
    with pytest.raises(ValidationError):
        CopilotScopeRequest.model_validate(
            {
                "data_merchant_id": "data-merchant",
                "template_id": "attacker-selected-template",
            }
        )


def test_ordinary_chat_session_rbac_is_unchanged():
    session = ChatSession(
        id="session-1",
        template_id="merchant-template",
        reseller_id="dashboard-reseller",
        merchant_id="dashboard-runtime",
    )

    validate_chat_session_access(
        _user(
            user_id="another-user",
            reseller_ids=["dashboard-reseller"],
            merchant_ids=["dashboard-runtime"],
        ),
        session,
        operation="send_message",
    )


def test_route_access_check_hides_copilot_scope_revalidation_failures(monkeypatch):
    session = ChatSession(
        id="session-1",
        template_id="dashboard-template",
        reseller_id="dashboard-reseller",
        merchant_id="dashboard-runtime",
        metadata={"copilot": {"data": {"data_merchant_id": "data-merchant"}}},
    )

    async def deny_copilot_scope(_metadata, _current_user):
        raise CopilotScopeError(
            "unauthorized_merchant",
            "Access denied to the stored Copilot data merchant.",
            status_code=404,
        )

    monkeypatch.setattr(
        chat_router,
        "validate_persisted_copilot_scope_access",
        deny_copilot_scope,
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            chat_router._validate_chat_and_copilot_session_access(
                _user(
                    reseller_ids=["dashboard-reseller"],
                    merchant_ids=["dashboard-runtime"],
                ),
                session,
                operation="get_session",
            )
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Chat session not found"


def test_send_message_handler_awaits_async_access_check(monkeypatch):
    class FakeLock:
        def __init__(self, *_args, **_kwargs):
            self.released = False

        async def acquire(self):
            return None

        async def release(self):
            self.released = True

    session = ChatSession(
        id="session-1",
        template_id="dashboard-template",
        reseller_id="dashboard-reseller",
        merchant_id="dashboard-runtime",
    )
    called: dict[str, bool] = {}

    async def load_session(_session_id: str):
        return session

    async def deny(_session: ChatSession):
        called["awaited"] = True
        raise HTTPException(status_code=404, detail="Chat session not found")

    monkeypatch.setattr(chat_handlers, "RedisLock", FakeLock)
    monkeypatch.setattr(chat_handlers, "get_chat_session_by_id", load_session)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            chat_handlers.send_chat_message_handler(
                "session-1",
                SendChatMessageRequest(content="hello"),
                access_check=deny,
            )
        )

    assert called["awaited"] is True
    assert exc.value.status_code == 404
    assert exc.value.detail == "Chat session not found"
