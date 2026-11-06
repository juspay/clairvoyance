"""serve_session_intent — the shared HTTP-layer intent router (RFC-001
§3.3 Stage B).

Covers the surface-independent policy seams the widget /intent route and
the demo router both ride: catalog gating, CLIENT rejection, and the
voice-live rule (DIRECT executes during a live voice attachment;
AGENT_TURN 409s with the typed ``voice_live`` code). The heavy handlers
(``send_chat_intent_handler`` / ``send_chat_message_handler``) are
monkeypatched — their internals are covered by the run_direct_intent
tests; here only the routing decision is under test.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict

import pytest
from fastapi import HTTPException

import app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.intents  # noqa: F401
from app.ai.voice.agents.breeze_buddy.chat.intents.router import ensure_flavor_intents
from app.ai.voice.agents.breeze_buddy.template.types import UiCatalogConfig
from app.api.routers.breeze_buddy.chat import handlers as ch

ensure_flavor_intents(["commerce"])


def _template() -> Any:
    return SimpleNamespace(
        id="t1",
        configurations=SimpleNamespace(
            ui_catalog=UiCatalogConfig(enabled_groups=["core", "commerce"]),
        ),
    )


def _session(catalog_version: str = "v2") -> SimpleNamespace:
    return SimpleNamespace(
        template_id="t1",
        metadata={"widget": {"catalog_version": catalog_version}},
    )


@pytest.fixture()
def routed(monkeypatch):
    """Patch the terminal handlers; record which route served the intent."""
    calls: Dict[str, Any] = {}

    async def _get_template(template_id):
        return _template()

    async def _direct(session_id, parsed, context=None, access_check=None):
        calls["direct"] = parsed.intent.intent
        # The piggybacked context patch must reach the direct path too —
        # it applies before dispatch so injection sees fresh identifiers.
        calls["direct_context"] = context
        return "DIRECT_SSE"

    async def _agent(session_id, req, access_check=None, internal=False):
        calls["agent_turn"] = req.content
        calls["internal"] = internal
        return "AGENT_SSE"

    monkeypatch.setattr(ch, "get_template_by_id_cached", _get_template)
    monkeypatch.setattr(ch, "send_chat_intent_handler", _direct)
    monkeypatch.setattr(ch, "send_chat_message_handler", _agent)
    return calls


def _add_to_cart() -> Dict[str, Any]:
    return {
        "intent": "add_to_cart",
        "component_id": "pg1",
        "payload": {"variant_id": "gid://shopify/ProductVariant/1"},
    }


def _view_product() -> Dict[str, Any]:
    return {
        "intent": "view_product",
        "component_id": "pg1",
        "payload": {"product_id": "gid://shopify/Product/1", "title": "Shoe"},
    }


@pytest.fixture
def agent_turn_view_product(monkeypatch):
    """view_product is DIRECT since the detail-overlay work — re-pin it to
    an AGENT_TURN policy for the router-branch tests below (monkeypatch
    restores the real policy afterwards; the global table stays clean)."""
    from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.intents import (
        ViewProductPayload,
    )
    from app.ai.voice.agents.breeze_buddy.chat.intents.router import (
        INTENT_POLICY,
        IntentPolicy,
        IntentRoute,
    )

    def _turn(parsed):
        payload = parsed.payload
        return f"Tell me about {payload.title} ({payload.product_id})"

    monkeypatch.setitem(
        INTENT_POLICY,
        "view_product",
        IntentPolicy(
            IntentRoute.AGENT_TURN,
            ViewProductPayload,
            flavor="commerce",
            agent_turn=_turn,
        ),
    )


async def test_direct_intent_executes_during_voice(routed):
    result = await ch.serve_session_intent(
        _session(), "s1", _add_to_cart(), voice_live=True
    )
    assert result == "DIRECT_SSE"
    assert routed["direct"] == "add_to_cart"


async def test_agent_turn_intent_409s_during_voice(routed, agent_turn_view_product):
    with pytest.raises(HTTPException) as exc:
        await ch.serve_session_intent(
            _session(), "s1", _view_product(), voice_live=True
        )
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert isinstance(detail, dict) and detail["code"] == "voice_live"
    assert "agent_turn" not in routed


async def test_agent_turn_intent_serves_normally_without_voice(
    routed, agent_turn_view_product
):
    result = await ch.serve_session_intent(_session(), "s1", _view_product())
    assert result == "AGENT_SSE"
    assert routed["agent_turn"] == "Tell me about Shoe (gid://shopify/Product/1)"
    # A plain (non-internal) AGENT_TURN policy must not opt into internal
    # persistence.
    assert routed["internal"] is False


def _enrich_product() -> Dict[str, Any]:
    return {
        "intent": "enrich_product",
        "component_id": "pd1",
        "payload": {"product_id": "gid://shopify/Product/1", "title": "Shoe"},
    }


async def test_enrich_product_routes_agent_turn_with_internal_persistence(routed):
    """The real (unpatched) enrich_product policy: AGENT_TURN + the
    internal flag plumbed through to send_chat_message_handler."""
    result = await ch.serve_session_intent(_session(), "s1", _enrich_product())
    assert result == "AGENT_SSE"
    assert routed["internal"] is True
    assert "Shoe" in routed["agent_turn"]
    assert "Do not call any tools" in routed["agent_turn"]


async def test_enrich_product_409s_during_voice(routed):
    with pytest.raises(HTTPException) as exc:
        await ch.serve_session_intent(
            _session(), "s1", _enrich_product(), voice_live=True
        )
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert isinstance(detail, dict) and detail["code"] == "voice_live"
    assert "agent_turn" not in routed


async def test_v1_session_gets_typed_catalog_422(routed):
    with pytest.raises(HTTPException) as exc:
        await ch.serve_session_intent(_session("v1"), "s1", _add_to_cart())
    assert exc.value.status_code == 422
    detail = exc.value.detail
    assert isinstance(detail, dict) and detail["code"] == "catalog_version_unsupported"


async def test_client_intent_gets_typed_422(routed):
    with pytest.raises(HTTPException) as exc:
        await ch.serve_session_intent(
            _session(),
            "s1",
            {"intent": "checkout", "component_id": "cv1", "payload": {}},
        )
    assert exc.value.status_code == 422
    detail = exc.value.detail
    assert isinstance(detail, dict) and detail["code"] == "client_side_intent"
