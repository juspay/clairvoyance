"""N-WID-3 — a paused widget previews only behind the console login.

Before: an ``Origin: https://breezebuddy.ai`` header let any client open a
session on a PAUSED widget and skip the store's allow-list. Any non-browser
client can set that header. Now the bypass needs a preview grant, minted
by ``POST /widget-config/{id}/preview`` behind RBAC, and the session's
widget token carries a server-signed ``preview`` claim instead.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from app.api.routers.breeze_buddy import widget_common
from app.api.routers.breeze_buddy.widget_config import handlers as config_handlers
from app.api.security.breeze_buddy.rbac_token import rbac_token_manager
from app.api.security.breeze_buddy.widget_token import (
    TYP_WIDGET_PREVIEW,
    is_preview_grant_for,
    mint_preview_grant,
    mint_widget_token,
    require_widget_session,
)
from app.core.security.jwt import jwt_manager
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.widget_config import WidgetConfigResponse

WIDGET = "00000000-0000-0000-0000-0000000000aa"
OTHER_WIDGET = "00000000-0000-0000-0000-0000000000bb"
CONSOLE = "https://breezebuddy.ai"
STORE = "https://store.example"


def _cfg(*, active: bool) -> WidgetConfigResponse:
    return WidgetConfigResponse(
        id=WIDGET,
        reseller_id="buddy-assist",
        merchant_id="store.myshopify.com",
        public_widget_key="public-key-123",
        template_id="00000000-0000-0000-0000-000000000002",
        allowed_origins=[STORE],
        active=active,
    )


def _request(origin: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/agent/voice/breeze-buddy/widget/session",
            "headers": [(b"origin", origin.encode())],
            "client": ("203.0.113.9", 51234),
            "query_string": b"",
        }
    )


@pytest.fixture()
def widget(monkeypatch):
    def install(cfg: WidgetConfigResponse) -> None:
        async def by_key(_key: str):
            return cfg

        async def no_limit(**_kwargs) -> None:
            return None

        monkeypatch.setattr(widget_common, "get_widget_config_by_public_key", by_key)
        monkeypatch.setattr(widget_common, "_enforce_widget_ip_limit", no_limit)

    return install


async def _create(origin: str, grant: Optional[str] = None):
    return await widget_common.resolve_widget_config_for_request(
        request=_request(origin),
        public_widget_key="public-key-123",
        rate_bucket="session_create",
        preview_grant=grant,
    )


# -- the bug ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_console_origin_alone_no_longer_opens_a_paused_widget(widget):
    widget(_cfg(active=False))
    with pytest.raises(HTTPException) as raised:
        await _create(CONSOLE)
    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_console_origin_alone_no_longer_skips_the_allow_list(widget):
    widget(_cfg(active=True))
    with pytest.raises(HTTPException) as raised:
        await _create(CONSOLE)
    assert raised.value.status_code == 403


# -- the console path ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_grant_previews_a_paused_widget_from_the_console(widget):
    widget(_cfg(active=False))
    cfg, preview = await _create(CONSOLE, mint_preview_grant(widget_config_id=WIDGET))
    assert (cfg.id, preview) == (WIDGET, True)


@pytest.mark.asyncio
async def test_a_grant_for_another_widget_does_not(widget):
    widget(_cfg(active=False))
    with pytest.raises(HTTPException) as raised:
        await _create(CONSOLE, mint_preview_grant(widget_config_id=OTHER_WIDGET))
    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_storefronts_are_unchanged(widget):
    widget(_cfg(active=True))
    cfg, preview = await _create(STORE)
    assert (cfg.id, preview) == (WIDGET, False)


# -- the grant itself ------------------------------------------------------


def test_grant_is_bound_to_one_widget_and_its_type():
    grant = mint_preview_grant(widget_config_id=WIDGET)
    assert is_preview_grant_for(grant, WIDGET)
    assert not is_preview_grant_for(grant, OTHER_WIDGET)
    assert not is_preview_grant_for(None, WIDGET)
    assert not is_preview_grant_for("not-a-jwt", WIDGET)
    session_token = mint_widget_token(widget_session_id="s1", widget_config_id=WIDGET)
    assert not is_preview_grant_for(session_token, WIDGET)


def test_expired_grant_is_refused():
    expired = jwt_manager.create_access_token(
        {"sub": "x", "typ": TYP_WIDGET_PREVIEW, "widget_config_id": WIDGET},
        expires_delta=timedelta(seconds=-1),
    )
    assert not is_preview_grant_for(expired, WIDGET)


def test_grant_is_neither_a_session_token_nor_a_console_login():
    grant = mint_preview_grant(widget_config_id=WIDGET)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=grant)
    with pytest.raises(HTTPException):
        require_widget_session(session_id="s1", creds=creds)
    with pytest.raises(HTTPException):
        rbac_token_manager.verify_rbac_token(grant)


def test_preview_claim_rides_the_session_token():
    def ctx(token: str):
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        return require_widget_session(session_id="s1", creds=creds)

    plain = mint_widget_token(widget_session_id="s1", widget_config_id=WIDGET)
    preview = mint_widget_token(
        widget_session_id="s1", widget_config_id=WIDGET, preview=True
    )
    assert ctx(plain).preview is False
    assert ctx(preview).preview is True


# -- the RBAC endpoint -----------------------------------------------------


def _user(role: UserRole, merchant_ids: list[str]) -> UserInfo:
    return UserInfo(
        id="u1",
        username="u1",
        role=role,
        reseller_ids=["buddy-assist"],
        merchant_ids=merchant_ids,
        permissions=[],
    )


@pytest.mark.asyncio
async def test_endpoint_mints_for_the_owning_merchant(monkeypatch):
    async def by_id(_id: str):
        return _cfg(active=False)

    monkeypatch.setattr(config_handlers, "get_widget_config_by_id", by_id)
    out = await config_handlers.mint_preview_grant_handler(
        WIDGET, _user(UserRole.MERCHANT, ["store.myshopify.com"])
    )
    assert is_preview_grant_for(out.preview_grant, WIDGET)
    assert out.ttl_seconds == 15 * 60


@pytest.mark.asyncio
async def test_endpoint_refuses_another_merchant(monkeypatch):
    async def by_id(_id: str):
        return _cfg(active=False)

    monkeypatch.setattr(config_handlers, "get_widget_config_by_id", by_id)
    with pytest.raises(HTTPException) as raised:
        await config_handlers.mint_preview_grant_handler(
            WIDGET, _user(UserRole.MERCHANT, ["other.myshopify.com"])
        )
    assert raised.value.status_code == 403
