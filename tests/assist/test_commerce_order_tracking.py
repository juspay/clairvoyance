"""Order tracking as a flag: ``flavor.ucp.features.order_tracking``.

Layers under test:
- core ``template/flavor_functions.py``: the generic provider hook the
  builder reads; core never names a tool.
- commerce ``ucp/order_tracking.py``: the switch, the two builtin entries,
  the handlers, the ``order_lookup`` seam.
- Shopify ``platforms/shopify/order_tracking.py``: the nautilus lookup.
"""

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import httpx
import pytest

import app.ai.voice.agents.breeze_buddy.assist.commerce  # noqa: F401 — registers the flavor
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp import (
    order_tracking as commerce_ot,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.hooks import (
    OrderLookupUnavailable,
    resolve_order_lookup,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.shopify import (
    order_tracking as shopify_ot,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce import (
    order_tracking as woo_ot,
)
from app.ai.voice.agents.breeze_buddy.chat.client_context import diff_state_patch
from app.ai.voice.agents.breeze_buddy.chat.tools.result_normalizer import normalize
from app.ai.voice.agents.breeze_buddy.handlers.internal.builtin_dispatcher import (
    BUILTIN_HANDLERS,
    register_builtin_handler,
)
from app.ai.voice.agents.breeze_buddy.template import flavor_functions
from app.ai.voice.agents.breeze_buddy.template.builder import FlowConfigBuilder
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    GlobalBuiltinFunction,
)

ORDER_STATUS_TOOL = commerce_ot.ORDER_STATUS_TOOL
PAGE_READ_TOOL = commerce_ot.PAGE_READ_TOOL
SHOP = "acme-1234.myshopify.com"
ORDER = {
    "order_name": "#110145",
    "order_number": 110145,
    "created_at": "2026-08-24T18:03:17+05:30",
    "fulfillment_status": "fulfilled",
    "line_items": ["Rawdha Attar – Madina-Inspired Signature Blend, 12ml"],
    "tracking_company": "Shadowfax DS 1kg",
    "tracking_number": "SF1876236403KAD",
    "tracking_url": "https://shiprocket.co/tracking/SF1876236403KAD",
    "shipment_status": "confirmed",
    "order_status_url": "https://amirandsons.com/x/orders/tok/authenticate?key=k",
}


def _configurations(
    flag: Optional[bool],
    connectors=("shopify",),
    groups=("core", "commerce"),
    ui_tools: Optional[Dict[str, str]] = None,
) -> ConfigurationModel:
    raw: Dict[str, Any] = {
        "ui_catalog": {"enabled_groups": list(groups)},
    }
    if ui_tools is not None:
        raw["ui_intents"] = {"tools": ui_tools}
    if flag is not None:
        raw["flavor"] = {
            "ucp": {
                "connectors": list(connectors),
                "features": {"upsell": True, "order_tracking": flag},
            }
        }
    return ConfigurationModel.model_validate(raw)


def _bot(flag: Optional[bool], configurations=None, **template_vars: Any):
    template = SimpleNamespace(
        id="tpl-1",
        merchant_id=SHOP,
        configurations=configurations or _configurations(flag),
    )
    return SimpleNamespace(
        template=template, template_vars=template_vars, agent_state={}
    )


def _context(flag: bool = True, configurations=None, **template_vars: Any) -> Any:
    bot = _bot(flag, configurations, **template_vars)
    return SimpleNamespace(
        bot=bot, call_sid="c1", configurations=bot.template.configurations
    )


def _context_after_lookup(tracking_url: Any = ORDER["tracking_url"]) -> Any:
    context = _context()
    context.bot.agent_state["tracking_url"] = tracking_url
    return context


def _mock_http(monkeypatch, module, handler):
    seen: List[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        module,
        "create_http_client",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    return seen


# ---------------------------------------------------------------------------
# core: the provider hook
# ---------------------------------------------------------------------------


class TestFlavorFunctionsHook:
    def test_provider_runs_only_for_an_enabled_group(self, monkeypatch):
        monkeypatch.setattr(flavor_functions, "_PROVIDERS", [])
        calls = []
        flavor_functions.register_flavor_functions(
            "other", lambda bot: calls.append(bot) or [{"name": "x"}]
        )
        assert flavor_functions.synthesize_flavor_functions(_bot(True)) == []
        assert calls == []
        bot = _bot(True, _configurations(True, groups=("core", "other")))
        assert flavor_functions.synthesize_flavor_functions(bot) == [[{"name": "x"}]]

    def test_registration_is_idempotent_for_the_same_function(self, monkeypatch):
        monkeypatch.setattr(flavor_functions, "_PROVIDERS", [])
        fn = lambda bot: []  # noqa: E731
        flavor_functions.register_flavor_functions("g", fn)
        flavor_functions.register_flavor_functions("g", fn)
        assert flavor_functions._PROVIDERS == [("g", fn)]

    def test_a_raising_provider_is_skipped(self, monkeypatch):
        monkeypatch.setattr(flavor_functions, "_PROVIDERS", [])

        def boom(bot):
            raise RuntimeError("x")

        flavor_functions.register_flavor_functions("commerce", boom)
        assert flavor_functions.synthesize_flavor_functions(_bot(True)) == []

    def test_a_declared_name_keeps_the_template_own_set(self):
        # Explicit declarations win, as for the KB tool, and a provider whose
        # name is taken adds none of its entries.
        declared = [{"name": "a", "mine": True}, {"name": "x"}]
        contributed = [[{"name": "a"}, {"name": "b"}], [{"name": "c"}]]
        assert flavor_functions.append_flavor_functions(declared, contributed) == [
            {"name": "a", "mine": True},
            {"name": "x"},
            {"name": "c"},
        ]

    def test_a_reserved_name_leaves_out_its_whole_provider(self):
        contributed = [[{"name": "a"}, {"name": "b"}], [{"name": "c"}]]
        assert flavor_functions.append_flavor_functions(
            [{"name": "x"}], contributed, reserved={"a"}
        ) == [{"name": "x"}, {"name": "c"}]

    def test_builtin_registration_refuses_a_taken_name(self):
        register_builtin_handler(ORDER_STATUS_TOOL, BUILTIN_HANDLERS[ORDER_STATUS_TOOL])
        with pytest.raises(ValueError):
            register_builtin_handler(ORDER_STATUS_TOOL, lambda c, a: None)


# ---------------------------------------------------------------------------
# commerce: the switch and the entries
# ---------------------------------------------------------------------------


class TestSwitch:
    def test_off_by_default(self):
        assert not commerce_ot.order_tracking_enabled(_configurations(None))
        assert not commerce_ot.order_tracking_enabled(_configurations(False))
        assert commerce_ot.order_tracking_functions(_configurations(None)) == []

    def test_on_gives_two_valid_builtins_registered_in_core(self):
        entries = commerce_ot.order_tracking_functions(_configurations(True))
        assert [e["name"] for e in entries] == [ORDER_STATUS_TOOL, PAGE_READ_TOOL]
        for entry in entries:
            built = GlobalBuiltinFunction.model_validate(entry)
            assert BUILTIN_HANDLERS[built.handler] in (
                commerce_ot.get_order_status,
                commerce_ot.read_page_content,
            )

    def test_no_tools_when_no_connector_can_look_up_orders(self):
        # A connector with no order lookup, or none named: the flag adds
        # nothing. Registration order follows import order, so it must
        # never choose the platform.
        for connectors in (("other",), ()):
            configurations = _configurations(True, connectors=connectors)
            assert commerce_ot.order_tracking_functions(configurations) == []

    def test_own_role_tools_win_over_the_flag(self, monkeypatch):
        warnings: List[str] = []
        monkeypatch.setattr(
            commerce_ot, "logger", SimpleNamespace(warning=warnings.append)
        )
        renamed = _configurations(True, ui_tools={"order_status": "track_order"})
        assert commerce_ot.order_tracking_functions(renamed) == []
        assert len(warnings) == 1 and "track_order" in warnings[0]
        page_only = _configurations(True, ui_tools={"page_read": "fetch_page"})
        assert commerce_ot.order_tracking_functions(page_only) == []
        assert len(warnings) == 2 and "fetch_page" in warnings[1]
        # Flag off: nothing to add, so nothing to warn about.
        off = _configurations(False, ui_tools={"order_status": "track_order"})
        assert commerce_ot.order_tracking_functions(off) == []
        assert len(warnings) == 2
        # Roles mapped to the default names (Esthreall's block) change nothing.
        defaults = _configurations(
            True,
            ui_tools={
                "order_status": ORDER_STATUS_TOOL,
                "page_read": PAGE_READ_TOOL,
                "create_cart": "shopify_cart_create",
            },
        )
        entries = commerce_ot.order_tracking_functions(defaults)
        assert [e["name"] for e in entries] == [ORDER_STATUS_TOOL, PAGE_READ_TOOL]

    def test_flag_on_with_no_lookup_is_logged(self, monkeypatch):
        warnings: List[str] = []
        monkeypatch.setattr(
            commerce_ot, "logger", SimpleNamespace(warning=warnings.append)
        )
        configurations = _configurations(True, connectors=("other",))
        assert commerce_ot.order_tracking_functions(configurations) == []
        assert len(warnings) == 1 and "['other']" in warnings[0]

    def test_the_connector_seam_knows_each_platform(self):
        assert resolve_order_lookup(["shopify"]) == ("shopify", shopify_ot.lookup_order)
        assert resolve_order_lookup(["woocommerce"]) == (
            "woocommerce",
            woo_ot.lookup_order,
        )
        assert resolve_order_lookup(["other"]) is None
        assert resolve_order_lookup([]) is None and resolve_order_lookup(None) is None
        # The template's order wins, not the registration order.
        woo = ("woocommerce", woo_ot.lookup_order)
        assert resolve_order_lookup(["woocommerce", "shopify"]) == woo
        assert resolve_order_lookup(["shopify", "woocommerce"]) == (
            "shopify",
            shopify_ot.lookup_order,
        )


class TestBuilder:
    def _names(self, flow, bot):
        return [f.name for f in FlowConfigBuilder().build_global_functions(flow, bot)]

    def test_direct_mode_gets_the_tools_from_the_flag(self):
        flow = {"mode": "direct", "system_prompt": "x", "functions": []}
        assert self._names(flow, _bot(True)) == [ORDER_STATUS_TOOL, PAGE_READ_TOOL]
        assert self._names(flow, _bot(False)) == []

    def test_a_template_declared_tool_wins_over_the_flag(self):
        own = {
            "type": "http",
            "name": ORDER_STATUS_TOOL,
            "description": "mine",
            "http_request": {"url": "https://example.com/x", "method": "GET"},
        }
        flow = {"mode": "direct", "system_prompt": "x", "functions": [own]}
        functions = FlowConfigBuilder().build_global_functions(flow, _bot(True))
        # The template's own lookup stays; the flag adds neither tool.
        assert [f.name for f in functions] == [ORDER_STATUS_TOOL]
        assert functions[0].description.startswith("mine")

    def test_flow_mode_node_tool_wins_over_the_flag(self):
        # Node and global functions are joined without a name check (chat
        # _tools_schema, pipecat-flows). A node declaring one of the two
        # tools keeps the template's own WISMO: the flag adds neither, since
        # its page read only reads the URL its own lookup saved.
        node_tool = {
            "function_name": ORDER_STATUS_TOOL,
            "description": "the node's own",
            "properties": {},
        }
        flow = {
            "initial_node": "start",
            "nodes": [{"node_name": "start", "functions": [node_tool]}],
            "global_functions": [],
        }
        assert self._names(flow, _bot(True)) == []

    def test_commerce_group_off_means_no_tools_even_with_the_flag(self):
        bot = _bot(True, _configurations(True, groups=("core",)))
        flow = {"mode": "direct", "system_prompt": "x", "functions": []}
        assert self._names(flow, bot) == []


# ---------------------------------------------------------------------------
# get_order_status: commerce handler over the Shopify lookup
# ---------------------------------------------------------------------------


class TestGetOrderStatus:
    async def test_success_through_the_shopify_lookup(self, monkeypatch):
        seen = _mock_http(
            monkeypatch,
            shopify_ot,
            lambda r: httpx.Response(200, json={"found": True, "orders": [ORDER]}),
        )
        context = _context(wismo_secret="tok")
        result = await commerce_ot.get_order_status(
            context,
            {"orderNumber": "#es 110145", "phone": "+91 70698 60258", "email": None},
        )
        assert context.bot.agent_state["tracking_url"] == ORDER["tracking_url"]
        request = seen[0]
        assert request.headers["Authorization"] == "Bearer tok"
        assert dict(request.url.params) == {
            "shopDomain": SHOP,
            "orderNumber": "ES110145",
            "phone": "917069860258",
        }
        assert result["status"] == "success"
        assert result["data"]["orders"][0]["tracking_url"] == ORDER["tracking_url"]
        # Chat gives the model only ``data`` of a success: the hint must be there.
        seen_by_model = normalize(ORDER_STATUS_TOOL, result)
        assert "read_page_content" in seen_by_model["next"]
        assert "OrderStatus" in seen_by_model["next"]

    async def test_no_tracking_url_means_no_page_read_step(self, monkeypatch):
        order = {**ORDER, "tracking_url": None}
        _mock_http(
            monkeypatch,
            shopify_ot,
            lambda r: httpx.Response(200, json={"found": True, "orders": [order]}),
        )
        result = await commerce_ot.get_order_status(
            _context(wismo_secret="tok"), {"orderNumber": "110145", "email": "a@b.c"}
        )
        assert result["status"] == "success"
        assert "read_page_content" not in result["data"]["next"]

    async def test_shop_url_fallback_reads_the_template_never_the_payload(
        self, monkeypatch
    ):
        seen = _mock_http(
            monkeypatch,
            shopify_ot,
            lambda r: httpx.Response(200, json={"found": True, "orders": [ORDER]}),
        )
        context = _context(wismo_secret="tok", shop_url="from-payload.myshopify.com")
        context.bot.template.merchant_id = "merchant-123"
        context.bot.template.secrets = {"shop_url": "own-shop.myshopify.com"}
        await commerce_ot.get_order_status(
            context, {"orderNumber": "1", "phone": "9876543210"}
        )
        assert seen[0].url.params["shopDomain"] == "own-shop.myshopify.com"

    @pytest.mark.parametrize(
        "merchant_id, shop_url, expected",
        [
            (f"assist-{SHOP}", "own-shop.myshopify.com", SHOP),
            ("www.acme.com", "own-shop.myshopify.com", "own-shop.myshopify.com"),
            ("www.acme.com", " Own-Shop.MyShopify.com ", "own-shop.myshopify.com"),
            (
                "www.acme.com",
                "https://own-shop.myshopify.com/",
                "own-shop.myshopify.com",
            ),
            (f"Assist-{SHOP.upper()}", "thebeyondbound.com", SHOP),
            # Nautilus knows no storefront domain, so it is never sent.
            ("www.acme.com", "thebeyondbound.com", None),
        ],
    )
    def test_shop_domain_for_other_merchant_id_shapes(
        self, merchant_id, shop_url, expected
    ):
        context = _context()
        context.bot.template.merchant_id = merchant_id
        context.bot.template.secrets = {"shop_url": shop_url}
        assert shopify_ot._shop_domain(context) == expected

    async def test_a_storefront_domain_never_calls_nautilus(self, monkeypatch):
        seen = _mock_http(monkeypatch, shopify_ot, lambda r: httpx.Response(200))
        context = _context(wismo_secret="tok")
        context.bot.template.merchant_id = "www.acme.com"
        context.bot.template.secrets = {"shop_url": "thebeyondbound.com"}
        result = await commerce_ot.get_order_status(
            context, {"orderNumber": "1", "phone": "9876543210"}
        )
        assert result["error"] == "wismo_not_available"
        assert seen == []

    async def test_missing_identity_never_calls_out(self, monkeypatch):
        seen = _mock_http(monkeypatch, shopify_ot, lambda r: httpx.Response(200))
        results = [
            await commerce_ot.get_order_status(
                _context(wismo_secret="tok"), {"phone": "9876543210"}
            ),
            await commerce_ot.get_order_status(
                _context(wismo_secret="tok"), {"orderNumber": "1"}
            ),
            await commerce_ot.get_order_status(
                _context(wismo_secret="tok"), {"orderNumber": "1", "phone": "12345"}
            ),
        ]
        assert {r["error"] for r in results} == {"missing_identifier"}
        assert seen == []

    async def test_missing_secret_is_unavailable(self, monkeypatch):
        seen = _mock_http(monkeypatch, shopify_ot, lambda r: httpx.Response(200))
        result = await commerce_ot.get_order_status(
            _context(), {"orderNumber": "1", "phone": "9876543210"}
        )
        assert result["error"] == "wismo_not_available"
        assert seen == []

    async def test_no_connector_for_the_template_is_unavailable(self):
        context = _context(configurations=_configurations(True, connectors=("woo",)))
        context.bot.template_vars = {"wismo_secret": "tok"}
        result = await commerce_ot.get_order_status(
            context, {"orderNumber": "1", "phone": "9876543210"}
        )
        assert result["error"] == "wismo_not_available"

    @pytest.mark.parametrize(
        "status, body, expected",
        [
            (404, {"found": False, "error": "order_not_found"}, "order_not_found"),
            (403, {"found": False, "error": "identity_mismatch"}, "identity_mismatch"),
            (401, {"error": "Unauthorized"}, "Unauthorized"),
            (500, {"error": "boom"}, "wismo_not_available"),
        ],
    )
    async def test_failures_are_error_envelopes(
        self, monkeypatch, status, body, expected
    ):
        _mock_http(monkeypatch, shopify_ot, lambda r: httpx.Response(status, json=body))
        result = await commerce_ot.get_order_status(
            _context(wismo_secret="tok"), {"orderNumber": "1", "phone": "9876543210"}
        )
        assert result["status"] == "error"
        assert result["error"] == expected
        assert result["status_code"] == status
        assert "message" in result

    async def test_not_matched_uses_one_neutral_wording(self, monkeypatch):
        answers = iter(
            [
                {"found": False, "error": "order_not_found"},
                {"found": False, "error": "identity_mismatch"},
            ]
        )
        _mock_http(
            monkeypatch, shopify_ot, lambda r: httpx.Response(404, json=next(answers))
        )
        args = {"orderNumber": "1", "phone": "9876543210"}
        first = await commerce_ot.get_order_status(_context(wismo_secret="tok"), args)
        second = await commerce_ot.get_order_status(_context(wismo_secret="tok"), args)
        assert first["message"] == second["message"] == commerce_ot.NOT_MATCHED

    @pytest.mark.parametrize(
        "args, expected",
        [
            ({"orderNumber": "2", "phone": "9876543210"}, "order_not_found"),
            ({"orderNumber": "2"}, "missing_identifier"),
        ],
    )
    async def test_a_failed_lookup_clears_the_last_tracking_url(
        self, monkeypatch, args, expected
    ):
        _mock_http(
            monkeypatch,
            shopify_ot,
            lambda r: httpx.Response(404, json={"error": "order_not_found"}),
        )
        context = _context(wismo_secret="tok")
        saved = {"tracking_url": "https://t.example/previous"}
        context.bot.agent_state.update(saved)
        result = await commerce_ot.get_order_status(context, args)
        assert result["error"] == expected
        # Chat saves only changed keys, so the clear must be one of them.
        patch = diff_state_patch(saved, context.bot.agent_state)
        assert patch == {"tracking_url": None}
        page = await commerce_ot.read_page_content(context, {"url": "x"})
        assert page["error"] == "invalid_url"

    async def test_a_short_phone_beside_an_email_is_not_sent(self, monkeypatch):
        seen = _mock_http(
            monkeypatch,
            shopify_ot,
            lambda r: httpx.Response(200, json={"found": True, "orders": [ORDER]}),
        )
        await commerce_ot.get_order_status(
            _context(wismo_secret="tok"),
            {"orderNumber": "1", "phone": "43210", "email": "a@b.c"},
        )
        assert "phone" not in seen[0].url.params
        assert seen[0].url.params["email"] == "a@b.c"

    async def test_transport_error_is_unavailable(self, monkeypatch):
        def boom(request):
            raise httpx.ConnectError("down", request=request)

        _mock_http(monkeypatch, shopify_ot, boom)
        result = await commerce_ot.get_order_status(
            _context(wismo_secret="tok"), {"orderNumber": "1", "phone": "9876543210"}
        )
        assert result["error"] == "wismo_not_available"

    async def test_shopify_lookup_raises_unavailable_when_unconfigured(self):
        with pytest.raises(OrderLookupUnavailable):
            await shopify_ot.lookup_order(
                _context(), order_number="1", phone="9876543210", email=None
            )


# ---------------------------------------------------------------------------
# read_page_content
# ---------------------------------------------------------------------------


class TestReadPageContent:
    async def test_reads_through_the_reader_and_feeds_the_card_gate(self, monkeypatch):
        seen = _mock_http(
            monkeypatch,
            commerce_ot,
            lambda r: httpx.Response(200, text="Title: Tracking\n25 Aug"),
        )
        # The model's url is ignored: the reader gets the looked-up URL.
        result = await commerce_ot.read_page_content(
            _context_after_lookup(), {"url": "https://evil.example/x"}
        )
        assert seen[0].url == httpx.URL(f"https://r.jina.ai/{ORDER['tracking_url']}")
        assert seen[0].headers["X-Engine"] == "browser"
        assert result["status"] == "success"
        assert result["data"]["page_text"] == "Title: Tracking\n25 Aug"
        assert normalize(PAGE_READ_TOOL, result)["next"] == commerce_ot.NEXT_TRANSCRIBE

    async def test_refuses_anything_but_https(self, monkeypatch):
        seen = _mock_http(monkeypatch, commerce_ot, lambda r: httpx.Response(200))
        for url in ("http://x.example/t", "javascript:alert(1)", "", None):
            result = await commerce_ot.read_page_content(
                _context_after_lookup(url), {"url": ORDER["tracking_url"]}
            )
            assert result["error"] == "invalid_url"
        assert seen == []

    async def test_refuses_when_no_order_was_looked_up(self, monkeypatch):
        seen = _mock_http(monkeypatch, commerce_ot, lambda r: httpx.Response(200))
        result = await commerce_ot.read_page_content(
            _context(), {"url": ORDER["tracking_url"]}
        )
        assert result["error"] == "invalid_url"
        assert seen == []

    async def test_empty_or_failed_page_stands_the_first_card(self, monkeypatch):
        _mock_http(monkeypatch, commerce_ot, lambda r: httpx.Response(502, text=""))
        result = await commerce_ot.read_page_content(
            _context_after_lookup(), {"url": ORDER["tracking_url"]}
        )
        assert result["error"] == "page_not_readable"
        assert "first card stands" in result["next"]


def test_envelope_matches_the_http_tool_shape():
    from app.ai.voice.agents.breeze_buddy.template.session_state import (
        _is_tool_success,
        _unwrap_tool_payload,
    )

    ok = {"status": "success", "status_code": 200, "data": {"found": True}}
    bad = {"status": "error", "error": "order_not_found", "message": "no"}
    assert _is_tool_success(ok) and not _is_tool_success(bad)
    assert _unwrap_tool_payload(ok) == {"found": True}
    assert json.dumps(ok)


def test_first_card_render_names_the_page_read_step():
    # The model picks its next step from render_ui's result: a card with a
    # tracking_url and no page fields yet must point at the page read.
    from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.render_ui import (
        _summarize_commerce,
    )

    order = {"order_name": "#1", "tracking_url": "https://t.example/x"}
    first = _summarize_commerce("OrderStatus", {"order": order})
    assert first is not None and PAGE_READ_TOOL in first["next"]
    enriched = _summarize_commerce(
        "OrderStatus", {"order": order, "eta_display": "Fri"}
    )
    assert enriched is not None and "next" not in enriched
    untracked = _summarize_commerce("OrderStatus", {"order": {"order_name": "#1"}})
    assert untracked is not None and "next" not in untracked
