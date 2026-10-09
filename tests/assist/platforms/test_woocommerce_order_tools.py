"""The WooCommerce order-tracking builtins a template declares
(``woocommerce/wismo/order_tools.py``): registration, ``get_order_status`` around
the lookup, and ``read_page_content``."""

import json
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import pytest

import app.ai.voice.agents.breeze_buddy.assist.commerce  # noqa: F401 — registers the flavor
from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce.wismo import (
    order_tools,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce.wismo.order_tracking import (
    OrderLookupUnavailable,
)
from app.ai.voice.agents.breeze_buddy.chat.client_context import diff_state_patch
from app.ai.voice.agents.breeze_buddy.chat.tools.result_normalizer import normalize
from app.ai.voice.agents.breeze_buddy.handlers.internal.builtin_dispatcher import (
    BUILTIN_HANDLERS,
)
from app.ai.voice.agents.breeze_buddy.template.builder import FlowConfigBuilder
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    GlobalBuiltinFunction,
)

TRACKING_URL = "https://www.bluedart.com/track?trackNo=77983312643"
ORDER = {
    "order_name": "#4512",
    "order_number": 4512,
    "fulfillment_status": "fulfilled",
    "line_items": ["Neckband - Black"],
    "tracking_company": "Bluedart",
    "tracking_number": "77983312643",
    "tracking_url": TRACKING_URL,
    "shipment_status": None,
    "order_status_url": "https://www.shopyvision.com/my-account/view-order/4512/",
}
# The two entries a template declares (docs/widget/WOOCOMMERCE_ASSIST.md).
TEMPLATE_FUNCTIONS = [
    {
        "type": "builtin",
        "name": "get_order_status",
        "handler": "woocommerce_order_status",
        "description": "Live status of an order the shopper already placed.",
        "properties": {
            "orderNumber": {"type": "string"},
            "phone": {"type": "string", "nullable": True},
            "email": {"type": "string", "nullable": True},
        },
        "required": ["orderNumber"],
    },
    {
        "type": "builtin",
        "name": "read_page_content",
        "handler": "read_tracking_page",
        "description": "Read the courier tracking page as text.",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    },
]


def _context(tracking_url: Any = None) -> Any:
    configurations = ConfigurationModel.model_validate(
        {"ui_catalog": {"enabled_groups": ["core", "commerce"]}}
    )
    template = SimpleNamespace(
        id="tpl-1", merchant_id="shopyvision", configurations=configurations
    )
    bot = SimpleNamespace(template=template, template_vars={}, agent_state={})
    if tracking_url is not None:
        bot.agent_state["tracking_url"] = tracking_url
    return SimpleNamespace(bot=bot, call_sid="c1", configurations=configurations)


def _lookup_answers(monkeypatch, answer: Any) -> List[Dict[str, Any]]:
    """Stub the store lookup; ``answer`` is ``(status, body)`` or an
    exception to raise. Returns the calls it received."""
    calls: List[Dict[str, Any]] = []

    async def fake(context, **kwargs):
        calls.append(kwargs)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(order_tools, "lookup_order", fake)
    return calls


def _mock_reader(monkeypatch, handler) -> List[httpx.Request]:
    seen: List[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        order_tools,
        "create_http_client",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    return seen


# ---------------------------------------------------------------------------
# Registration: a template declares the two builtins itself
# ---------------------------------------------------------------------------


def test_the_declared_entries_reach_their_handlers():
    for entry in TEMPLATE_FUNCTIONS:
        built = GlobalBuiltinFunction.model_validate(entry)
        assert BUILTIN_HANDLERS[built.handler] in (
            order_tools.get_order_status,
            order_tools.read_page_content,
        )


def test_a_direct_mode_template_gets_both_tools():
    configurations = ConfigurationModel.model_validate(
        {"ui_catalog": {"enabled_groups": ["core", "commerce"]}}
    )
    bot = SimpleNamespace(
        template=SimpleNamespace(
            id="tpl-1", merchant_id="shopyvision", configurations=configurations
        ),
        template_vars={},
        agent_state={},
    )
    flow = {"mode": "direct", "system_prompt": "x", "functions": TEMPLATE_FUNCTIONS}
    names = [f.name for f in FlowConfigBuilder().build_global_functions(flow, bot)]
    assert names == ["get_order_status", "read_page_content"]


# ---------------------------------------------------------------------------
# get_order_status
# ---------------------------------------------------------------------------


async def test_success_saves_the_tracking_url_and_names_the_page_read(monkeypatch):
    calls = _lookup_answers(monkeypatch, (200, {"found": True, "orders": [ORDER]}))
    context = _context()
    result = await order_tools.get_order_status(
        context, {"orderNumber": " #4512 ", "phone": "+91 98765-43210"}
    )
    assert calls == [{"order_number": "4512", "phone": "919876543210", "email": None}]
    assert result["status"] == "success"
    assert result["data"]["orders"] == [ORDER]
    assert context.bot.agent_state["tracking_url"] == TRACKING_URL
    # Chat gives the model only ``data`` of a success, so ``next`` lives there.
    assert "read_page_content" in normalize("get_order_status", result)["next"]


async def test_no_tracking_url_means_no_page_read_step(monkeypatch):
    order = {**ORDER, "tracking_url": None}
    _lookup_answers(monkeypatch, (200, {"found": True, "orders": [order]}))
    result = await order_tools.get_order_status(
        _context(), {"orderNumber": "4512", "email": "a@b.c"}
    )
    assert result["data"]["next"] == order_tools.NEXT_RENDER


@pytest.mark.parametrize(
    "args",
    [
        {"phone": "9876543210"},
        {"orderNumber": "4512"},
        {"orderNumber": "4512", "phone": "12345"},
    ],
    ids=["no-order-number", "no-phone-or-email", "short-phone-alone"],
)
async def test_a_missing_detail_never_calls_the_store(monkeypatch, args):
    calls = _lookup_answers(monkeypatch, (200, {"found": True, "orders": [ORDER]}))
    result = await order_tools.get_order_status(_context(), args)
    assert result["error"] == "missing_identifier"
    assert calls == []


async def test_a_short_phone_beside_an_email_is_looked_up(monkeypatch):
    # The lookup ignores a phone under 10 digits, so the email decides.
    calls = _lookup_answers(monkeypatch, (200, {"found": True, "orders": [ORDER]}))
    result = await order_tools.get_order_status(
        _context(), {"orderNumber": "4512", "phone": "43210", "email": "a@b.c"}
    )
    assert result["status"] == "success" and calls[0]["email"] == "a@b.c"


@pytest.mark.parametrize(
    "answer",
    [
        (404, {"found": False, "error": "order_not_found"}),
        (403, {"found": False, "error": "identity_mismatch"}),
    ],
    ids=["not-found", "mismatch"],
)
async def test_no_match_looks_the_same_either_way(monkeypatch, answer):
    # A wrong detail and a missing order give the model the exact same result,
    # so it can never reveal whether an order number exists.
    _lookup_answers(monkeypatch, answer)
    result = await order_tools.get_order_status(
        _context(), {"orderNumber": "4512", "phone": "9876543210"}
    )
    assert result == {
        "status": "error",
        "error": "order_not_matched",
        "message": order_tools.NOT_MATCHED,
        "next": order_tools.NEXT_FIX_ARGS,
    }


async def test_an_unreachable_store_is_unavailable(monkeypatch):
    _lookup_answers(monkeypatch, OrderLookupUnavailable("store answered 401"))
    result = await order_tools.get_order_status(
        _context(), {"orderNumber": "4512", "phone": "9876543210"}
    )
    assert result["error"] == "wismo_not_available"
    assert result["message"] == order_tools.NOT_REACHABLE


@pytest.mark.parametrize(
    "args, answer",
    [
        (
            {"orderNumber": "2", "phone": "9876543210"},
            (404, {"found": False, "error": "order_not_found"}),
        ),
        ({"orderNumber": "2"}, None),
    ],
    ids=["failed-lookup", "missing-detail"],
)
async def test_any_call_clears_the_last_tracking_url(monkeypatch, args, answer):
    _lookup_answers(monkeypatch, answer)
    context = _context(tracking_url="https://t.example/previous")
    await order_tools.get_order_status(context, args)
    # Chat saves only changed keys, so the clear must be one of them.
    saved = {"tracking_url": "https://t.example/previous"}
    assert diff_state_patch(saved, context.bot.agent_state) == {"tracking_url": None}
    page = await order_tools.read_page_content(context, {"url": "x"})
    assert page["error"] == "invalid_url"


# ---------------------------------------------------------------------------
# read_page_content
# ---------------------------------------------------------------------------


async def test_reads_only_the_saved_url_through_the_reader(monkeypatch):
    seen = _mock_reader(
        monkeypatch, lambda r: httpx.Response(200, text="Shipment Delivered 08 Oct")
    )
    # The model's url is ignored: the reader gets the looked-up URL.
    result = await order_tools.read_page_content(
        _context(tracking_url=TRACKING_URL), {"url": "https://evil.example/x"}
    )
    assert seen[0].url == httpx.URL(f"https://r.jina.ai/{TRACKING_URL}")
    assert seen[0].headers["X-Engine"] == "browser"
    assert result["data"]["page_text"] == "Shipment Delivered 08 Oct"
    assert normalize("read_page_content", result)["next"] == order_tools.NEXT_TRANSCRIBE


async def test_refuses_anything_but_a_saved_https_url(monkeypatch):
    seen = _mock_reader(monkeypatch, lambda r: httpx.Response(200))
    for url in ("http://x.example/t", "javascript:alert(1)", "", None):
        result = await order_tools.read_page_content(
            _context(tracking_url=url), {"url": TRACKING_URL}
        )
        assert result["error"] == "invalid_url"
    assert seen == []


async def test_an_unreadable_page_keeps_the_first_card(monkeypatch):
    _mock_reader(monkeypatch, lambda r: httpx.Response(422, text="refused"))
    result = await order_tools.read_page_content(
        _context(tracking_url=TRACKING_URL), {"url": TRACKING_URL}
    )
    assert result["error"] == "page_not_readable"
    assert "first card stands" in result["next"]


def test_results_use_the_http_tool_envelope():
    from app.ai.voice.agents.breeze_buddy.template.session_state import (
        _is_tool_success,
        _unwrap_tool_payload,
    )

    ok = {"status": "success", "status_code": 200, "data": {"found": True}}
    bad = order_tools._error("order_not_found", "no")
    assert _is_tool_success(ok) and not _is_tool_success(bad)
    assert _unwrap_tool_payload(ok) == {"found": True}
    assert json.dumps(ok)
