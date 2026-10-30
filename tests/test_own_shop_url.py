"""N-WID-2 — a public session's ``shop_url`` is kept only when it names the store.

``shop_url`` is substituted into the commerce MCP URL
(``https://{shop_url}/api/ucp/mcp``), and widget/demo sessions are created
by anonymous clients. The helper is tested directly; the widget handler test
proves the guard is wired in before the session is persisted.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Request

from app.ai.voice.agents.breeze_buddy.assist.engine.identity import own_shop_url
from app.api.routers.breeze_buddy.widget import handlers as widget_handlers
from app.schemas.breeze_buddy.chat import CreateWidgetSessionRequest
from app.schemas.breeze_buddy.widget_config import WidgetConfigResponse

OWN = ["trustshop.co.in", "esxxqz-1c.myshopify.com", "https://www.trustshop.co.in"]


@pytest.mark.parametrize(
    "sent, kept",
    [
        ("esxxqz-1c.myshopify.com", "esxxqz-1c.myshopify.com"),
        ("https://ESXXQZ-1C.myshopify.com/", "esxxqz-1c.myshopify.com"),
        ("trustshop.co.in", "trustshop.co.in"),
        ("www.trustshop.co.in", "www.trustshop.co.in"),
        ("trustshop.co.in/products/x?y=1", "trustshop.co.in"),
        ("trustshop.co.in.", "trustshop.co.in"),
    ],
)
def test_own_domain_is_kept_as_a_bare_host(sent, kept):
    assert own_shop_url({"shop_url": sent, "x": 1}, OWN) == {"shop_url": kept, "x": 1}


@pytest.mark.parametrize(
    "sent",
    [
        "evil.example",
        "internal-svc.local",
        "trustshop.co.in.evil.example",
        "http://trustshop.co.in",
        "trustshop.co.in:8443",
        "esxxqz-1c",
        "******",
        "",
        None,
    ],
)
def test_anything_else_is_dropped(sent):
    assert own_shop_url({"shop_url": sent, "x": 1}, OWN) == {"x": 1}


@pytest.mark.parametrize("sent", ["evil.example", "http://trustshop.co.in", ""])
def test_a_dropped_value_falls_back_to_the_saved_shop_url(sent):
    # Without the fallback, session create would persist shop_url="" and that
    # would override the template's saved shop_url on every turn.
    assert own_shop_url({"shop_url": sent}, OWN, "https://trustshop.co.in/") == {
        "shop_url": "trustshop.co.in"
    }


def test_an_unparseable_saved_shop_url_is_not_used():
    assert own_shop_url({"shop_url": "evil.example"}, OWN, "******") == {}


def test_no_shop_url_and_no_vars_are_untouched():
    assert own_shop_url({"x": 1}, OWN) == {"x": 1}
    assert own_shop_url(None, OWN) == {}
    assert own_shop_url({"x": 1}, OWN, "trustshop.co.in") == {"x": 1}


def test_unparseable_own_domains_are_ignored():
    # e.g. a template copied with its secrets still masked
    assert own_shop_url({"shop_url": "a.example"}, ["******", None]) == {}
    assert own_shop_url({"shop_url": "a.example"}, ["******", "a.example"]) == {
        "shop_url": "a.example"
    }


class _Persisted(Exception):
    pass


async def _create_session_with(monkeypatch, sent_vars):
    cfg = WidgetConfigResponse(
        id="00000000-0000-0000-0000-000000000020",
        reseller_id="buddy-assist",
        merchant_id="esxxqz-1c.myshopify.com",
        public_widget_key="public-key-123",
        template_id="00000000-0000-0000-0000-000000000002",
        allowed_origins=["https://www.trustshop.co.in"],
        active=True,
    )
    template = SimpleNamespace(
        secrets={"shop_url": "trustshop.co.in"},
        merchant_id="esxxqz-1c.myshopify.com",
    )
    captured = {}

    async def _capture(create_req, *_args, **_kwargs):
        captured["template_vars"] = create_req.template_vars
        raise _Persisted

    monkeypatch.setattr(
        widget_handlers,
        "resolve_widget_config_for_request",
        AsyncMock(return_value=cfg),
    )
    monkeypatch.setattr(
        widget_handlers, "get_template_by_id_cached", AsyncMock(return_value=template)
    )
    monkeypatch.setattr(widget_handlers, "validate_template_for_chat", lambda _t: None)
    monkeypatch.setattr(widget_handlers, "negotiate_catalog", lambda _t, _v: ("v1", []))
    monkeypatch.setattr(widget_handlers, "create_chat_session_handler", _capture)

    body = CreateWidgetSessionRequest(
        public_widget_key="public-key-123", template_vars=sent_vars
    )
    request = Request({"type": "http", "headers": [], "method": "POST", "path": "/"})
    with pytest.raises(_Persisted):
        await widget_handlers.create_widget_session_handler(body, request)
    return captured["template_vars"]


@pytest.mark.asyncio
async def test_widget_session_replaces_a_foreign_shop_url_with_the_saved_one(
    monkeypatch,
):
    persisted = await _create_session_with(
        monkeypatch, {"shop_url": "evil.example", "shopify_customer_token": "t"}
    )
    assert persisted == {
        "shop_url": "trustshop.co.in",
        "shopify_customer_token": "t",
    }


@pytest.mark.asyncio
async def test_widget_session_keeps_the_stores_own_shop_url(monkeypatch):
    persisted = await _create_session_with(
        monkeypatch, {"shop_url": "esxxqz-1c.myshopify.com"}
    )
    assert persisted == {"shop_url": "esxxqz-1c.myshopify.com"}
