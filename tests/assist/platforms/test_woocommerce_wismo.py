"""WooCommerce order tracking: the lookup (``woocommerce/wismo/lookup.py``),
the ``get_order_status`` builtin (``woocommerce/wismo/handlers.py``) and the
shared page read it hands over to (``commerce/ucp/wismo.py``).

The store comes from the template's ``/mcp/woocommerce/<host>`` tool server
URL; the key from the plain credentials row the template names in
``flavor.ucp.credentials.woocommerce``.
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import pytest

import app.ai.voice.agents.breeze_buddy.assist.commerce  # noqa: F401 — registers the flavor
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp import wismo
from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce import store_api
from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce.wismo import (
    handlers,
    lookup as woo_ot,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce.wismo.lookup import (
    OrderLookupUnavailable,
)
from app.ai.voice.agents.breeze_buddy.chat.client_context import diff_state_patch
from app.ai.voice.agents.breeze_buddy.chat.flavors import resolve_flavor_scope
from app.ai.voice.agents.breeze_buddy.chat.tools.annotations import (
    is_read_only,
    resolve_tool_annotation,
)
from app.ai.voice.agents.breeze_buddy.chat.tools.result_normalizer import normalize
from app.ai.voice.agents.breeze_buddy.handlers.internal.builtin_dispatcher import (
    BUILTIN_HANDLERS,
)
from app.ai.voice.agents.breeze_buddy.template.builder import FlowConfigBuilder
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    GlobalBuiltinFunction,
)

# ===========================================================================
# The lookup
# ===========================================================================

HOST = "www.shopyvision.com"
TOOL_SERVER = f"https://api.breezebuddy.ai/mcp/woocommerce/{HOST}"
MERCHANT = "www.shopyvision.com"
CREDENTIAL_ID = "3f2a9c1e-7b4d-4e8a-9f0c-1d2e3f4a5b6c"
ACCOUNT = {"consumer_key": "ck_test", "consumer_secret": "cs_test"}
ORDER = {
    "id": 4512,
    "number": "4512",
    "status": "completed",
    "date_created": "2026-10-01T10:00:00",
    "billing": {"phone": "+91 98765 43210", "email": "Test@Example.com"},
    "line_items": [{"name": "Neckband - Black"}, {"name": "Mouse"}],
}
TRACKINGS = [
    {
        "tracking_provider": "Shiprocket",
        "tracking_number": "SR123",
        "tracking_link": "https://shiprocket.co/tracking/SR123",
    }
]


def _configurations(servers: Any = None, credential_id: Any = CREDENTIAL_ID):
    if servers is None:
        servers = [{"url": TOOL_SERVER}]
    credentials = {"woocommerce": credential_id} if credential_id else {}
    return ConfigurationModel.model_validate(
        {
            "mcp": {"servers": servers},
            "ui_catalog": {"enabled_groups": ["core", "commerce"]},
            "flavor": {"ucp": {"credentials": credentials}},
        }
    )


def _context(servers: Any = None, credential_id: Any = CREDENTIAL_ID) -> Any:
    configurations = _configurations(servers, credential_id)
    template = SimpleNamespace(
        reseller_id="BB_ASSIST",
        merchant_id=MERCHANT,
        configurations=configurations,
    )
    bot = SimpleNamespace(template=template, template_vars={}, agent_state={})
    return SimpleNamespace(bot=bot, call_sid="c1", configurations=configurations)


def _row(
    reseller_id: Any = "BB_ASSIST",
    merchant_id: Any = MERCHANT,
    is_active: bool = True,
    **value: Any,
) -> Any:
    """A plain credential row; a value of None drops that field."""
    fields = {k: v for k, v in {**ACCOUNT, **value}.items() if v is not None}
    return SimpleNamespace(
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        is_active=is_active,
        value=fields,
    )


@pytest.fixture
def egress(monkeypatch) -> List[str]:
    """The URLs passed to the SSRF check, in order."""
    checked: List[str] = []

    async def record(url):
        checked.append(url)
        return []

    monkeypatch.setattr(store_api, "validate_egress_url", record)
    return checked


@pytest.fixture
def rows(monkeypatch, egress) -> List[Any]:
    """The row the template names: the read returns rows[0], or None when
    empty (no such row, or a provider account, which placeholder_only
    hides)."""
    found: List[Any] = [_row()]

    async def fake_read(credential_id, mask=True, placeholder_only=False):
        assert (credential_id, mask, placeholder_only) == (CREDENTIAL_ID, False, True)
        return found[0] if found else None

    monkeypatch.setattr(woo_ot, "get_credential_by_id", fake_read)
    return found


def _store(monkeypatch, routes: Dict[str, httpx.Response]) -> List[httpx.Request]:
    seen: List[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return routes.get(request.url.path, httpx.Response(404, json=NO_ROUTE))

    monkeypatch.setattr(
        store_api,
        "create_http_client",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    return seen


ORDER_PATH = "/wp-json/wc/v3/orders/4512"
NO_SUCH_ORDER = {"code": "woocommerce_rest_shop_order_invalid_id"}
NO_ROUTE = {"code": "rest_no_route"}
TRACKING_PATH = "/wp-json/wc-shipment-tracking/v3/orders/4512/shipment-trackings"


async def _lookup(phone=None, email=None, number="4512", context=None):
    return await woo_ot.lookup_order(
        context or _context(), order_number=number, phone=phone, email=email
    )


async def test_found_with_tracking(rows, egress, monkeypatch):
    seen = _store(
        monkeypatch,
        {
            ORDER_PATH: httpx.Response(200, json=ORDER),
            TRACKING_PATH: httpx.Response(200, json=TRACKINGS),
        },
    )
    status, body = await _lookup(phone="9876543210")
    assert status == 200 and body["found"] is True
    assert body["orders"][0] == {
        "order_name": "#4512",
        "order_number": 4512,
        "created_at": "2026-10-01T10:00:00",
        "fulfillment_status": "fulfilled",
        "line_items": ["Neckband - Black", "Mouse"],
        "tracking_company": "Shiprocket",
        "tracking_number": "SR123",
        "tracking_url": "https://shiprocket.co/tracking/SR123",
        "shipment_status": None,
        "order_status_url": f"https://{HOST}/my-account/view-order/4512/",
    }
    assert [r.url.host for r in seen] == [HOST, HOST]
    # Every request passed the SSRF check first.
    assert egress == [str(r.url) for r in seen]
    assert seen[0].headers["Authorization"].startswith("Basic ")


async def test_a_store_without_the_tracking_plugin_still_answers(rows, monkeypatch):
    _store(monkeypatch, {ORDER_PATH: httpx.Response(200, json=ORDER)})
    status, body = await _lookup(email="test@example.com")
    assert status == 200 and body["orders"][0]["tracking_url"] is None


@pytest.mark.parametrize(
    "woo_status, fulfillment",
    [
        ("processing", "unfulfilled"),
        ("on-hold", "unfulfilled"),
        ("partial-shipped", "partial"),
        ("cancelled", "cancelled"),
        # Maybe unpaid, so not "being prepared": the card's neutral update.
        ("pending", "pending"),
    ],
)
async def test_status_mapping(rows, monkeypatch, woo_status, fulfillment):
    _store(
        monkeypatch,
        {ORDER_PATH: httpx.Response(200, json={**ORDER, "status": woo_status})},
    )
    _, body = await _lookup(phone="9876543210")
    assert body["orders"][0]["fulfillment_status"] == fulfillment


@pytest.mark.parametrize("woo_status", ["processing", "on-hold"])
async def test_a_shipment_on_an_open_order_reads_as_shipped(
    rows, monkeypatch, woo_status
):
    _store(
        monkeypatch,
        {
            ORDER_PATH: httpx.Response(200, json={**ORDER, "status": woo_status}),
            TRACKING_PATH: httpx.Response(200, json=TRACKINGS),
        },
    )
    _, body = await _lookup(phone="9876543210")
    assert body["orders"][0]["fulfillment_status"] == "fulfilled"


async def test_a_shipment_row_without_a_number_or_link_is_no_shipment(
    rows, monkeypatch
):
    _store(
        monkeypatch,
        {
            ORDER_PATH: httpx.Response(200, json={**ORDER, "status": "processing"}),
            TRACKING_PATH: httpx.Response(200, json=[{"date_shipped": "2026-10-01"}]),
        },
    )
    _, body = await _lookup(phone="9876543210")
    order = body["orders"][0]
    assert order["fulfillment_status"] == "unfulfilled"
    assert order["tracking_number"] is None and order["tracking_url"] is None


async def test_plugin_values_reach_the_card_as_text(rows, monkeypatch):
    # The card's fields are strings; a plugin may send a number.
    _store(
        monkeypatch,
        {
            ORDER_PATH: httpx.Response(200, json=ORDER),
            TRACKING_PATH: httpx.Response(
                200, json=[{"tracking_number": 77983312643, "tracking_provider": 7}]
            ),
        },
    )
    _, body = await _lookup(phone="9876543210")
    order = body["orders"][0]
    assert (order["tracking_number"], order["tracking_company"]) == ("77983312643", "7")
    assert wismo.OrderP.model_validate(order).tracking_number == "77983312643"


async def test_leading_zeros_are_dropped_before_the_read(rows, monkeypatch):
    seen = _store(monkeypatch, {ORDER_PATH: httpx.Response(200, json=ORDER)})
    status, _ = await _lookup(phone="9876543210", number="004512")
    assert status == 200 and seen[0].url.path == ORDER_PATH
    calls = len(seen)
    status, body = await _lookup(phone="9876543210", number="000")
    assert (status, body["error"]) == (404, "order_not_found")
    assert len(seen) == calls  # all zeros never calls the store


def test_both_wismo_roles_are_pure_reads():
    """Both may fan out: a lookup only adds its link to the saved list, and
    the page read picks the link the model names from that list, so two
    lookups in one batch never share a page."""
    scope = resolve_flavor_scope(_context().bot.template, ["commerce"])
    assert resolve_tool_annotation("get_order_status", None, scope) == "read_only"
    assert is_read_only("get_order_status", None, scope)
    assert is_read_only("read_page_content", None, scope)


@pytest.mark.parametrize(
    "tracking",
    [httpx.ConnectError("down"), httpx.Response(200, text="<html>blocked</html>")],
    ids=["unreachable", "not-json"],
)
async def test_a_failed_tracking_call_still_returns_the_order(
    rows, monkeypatch, tracking
):
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == ORDER_PATH:
            return httpx.Response(200, json=ORDER)
        if isinstance(tracking, Exception):
            raise tracking
        return tracking

    monkeypatch.setattr(
        store_api,
        "create_http_client",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    status, body = await _lookup(phone="9876543210")
    assert status == 200 and body["orders"][0]["tracking_url"] is None


async def test_identity_rules(rows, monkeypatch):
    seen = _store(monkeypatch, {ORDER_PATH: httpx.Response(200, json=ORDER)})
    assert (await _lookup(phone="1111111111"))[1]["error"] == "identity_mismatch"
    assert (await _lookup(email="other@example.com"))[1]["error"] == "identity_mismatch"
    # Tracking is read only after the identity check passes.
    assert [r.url.path for r in seen[:2]] == [ORDER_PATH, ORDER_PATH]
    assert (await _lookup(phone="919876543210"))[0] == 200
    # A short phone beside a matching email: the email decides.
    assert (await _lookup(phone="43210", email="test@example.com"))[0] == 200
    # A phone that does not match falls back to the email.
    assert (await _lookup(phone="1111111111", email="test@example.com"))[0] == 200


async def test_the_shipping_phone_also_matches(rows, monkeypatch):
    order = {**ORDER, "shipping": {"phone": "099999 00000"}}
    _store(monkeypatch, {ORDER_PATH: httpx.Response(200, json=order)})
    assert (await _lookup(phone="9999900000"))[0] == 200
    assert (await _lookup(phone="1111111111"))[1]["error"] == "identity_mismatch"


async def test_not_found_cases(rows, monkeypatch):
    seen = _store(
        monkeypatch,
        {
            ORDER_PATH: httpx.Response(200, json={**ORDER, "number": "WEB-77"}),
            "/wp-json/wc/v3/orders/9999": httpx.Response(404, json=NO_SUCH_ORDER),
        },
    )
    assert (await _lookup(phone="9876543210", number="ES4512"))[1]["error"] == (
        "order_not_found"
    )
    assert seen == []  # a non-numeric number never calls the store
    # The ID exists but shows another number (a numbering plugin).
    assert (await _lookup(phone="9876543210"))[1]["error"] == "order_not_found"
    # No order with this ID.
    assert (await _lookup(phone="9876543210", number="9999"))[1]["error"] == (
        "order_not_found"
    )
    # One exact read each, never the slow free-text search.
    assert [r.url.path for r in seen] == [ORDER_PATH, "/wp-json/wc/v3/orders/9999"]


async def test_a_firewall_html_404_is_unavailable_and_named(rows, monkeypatch):
    _store(monkeypatch, {ORDER_PATH: httpx.Response(404, text="<html>blocked</html>")})
    with pytest.raises(woo_ot.OrderLookupUnavailable, match="no orders route"):
        await _lookup(phone="9876543210")


async def test_a_missing_orders_route_is_unavailable_not_not_found(rows, monkeypatch):
    # A 404 without WooCommerce's "no such order" code: the REST API is off
    # or the URL is wrong. Saying "order not found" would mislead the shopper.
    _store(monkeypatch, {ORDER_PATH: httpx.Response(404, json=NO_ROUTE)})
    with pytest.raises(woo_ot.OrderLookupUnavailable):
        await _lookup(phone="9876543210")


async def test_a_malformed_billing_never_matches(rows, monkeypatch):
    _store(
        monkeypatch,
        {ORDER_PATH: httpx.Response(200, json={**ORDER, "billing": ["x"]})},
    )
    assert (await _lookup(phone="9876543210"))[1]["error"] == "identity_mismatch"


async def test_non_ascii_digits_are_not_found_without_a_call(rows, monkeypatch):
    seen = _store(monkeypatch, {ORDER_PATH: httpx.Response(200, json=ORDER)})

    async def no_rows(*args, **kwargs):
        raise AssertionError("credentials read for an invalid order number")

    monkeypatch.setattr(woo_ot, "get_credential_by_id", no_rows)
    status, body = await _lookup(phone="9876543210", number="\u0664\u0665\u0661\u0662")

    assert status == 404 and body["error"] == "order_not_found"
    assert seen == []


async def test_a_slow_store_hits_the_lookup_deadline(rows, monkeypatch):
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=ORDER)

    monkeypatch.setattr(woo_ot, "ORDER_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(
        store_api,
        "create_http_client",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(slow)),
    )
    with pytest.raises(woo_ot.OrderLookupUnavailable, match="did not answer"):
        await _lookup(phone="9876543210")


async def test_a_slow_tracking_read_still_returns_the_order(rows, monkeypatch):
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == TRACKING_PATH:
            await asyncio.sleep(5)
            return httpx.Response(200, json=TRACKINGS)
        return httpx.Response(200, json=ORDER)

    monkeypatch.setattr(woo_ot, "TRACKING_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(
        store_api,
        "create_http_client",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    status, body = await _lookup(phone="9876543210")
    assert status == 200 and body["orders"][0]["tracking_url"] is None


@pytest.mark.parametrize(
    "found",
    [
        [],
        [_row(is_active=False)],
        [_row(reseller_id="OTHER_RESELLER")],
        [_row(merchant_id="another-merchant")],
        [_row(merchant_id=None)],
        [_row(reseller_id=None, merchant_id=None)],
        [_row(consumer_key=" ")],
        [_row(consumer_secret="")],
        [_row(extra="x")],
    ],
    ids=[
        "missing-or-provider-row",
        "inactive",
        "other-reseller",
        "other-merchant",
        "reseller-wide",
        "global",
        "blank-key",
        "no-secret",
        "extra-field",
    ],
)
async def test_an_unusable_account_never_calls_the_store(rows, monkeypatch, found):
    rows[:] = found
    seen = _store(monkeypatch, {ORDER_PATH: httpx.Response(200, json=ORDER)})
    with pytest.raises(woo_ot.OrderLookupUnavailable):
        await _lookup(phone="9876543210")
    assert seen == []


@pytest.mark.parametrize(
    "servers",
    [
        [],
        [{"url": "https://www.shopyvision.com/api/ucp/mcp"}],
        [{"url": "https://api.breezebuddy.ai/mcp/woocommerce/{shop_url}"}],
        [{"url": TOOL_SERVER, "enabled": False}],
        [
            {"url": TOOL_SERVER},
            {"url": "https://api.breezebuddy.ai/mcp/woocommerce/x.com"},
        ],
    ],
    ids=[
        "no-tool-server",
        "not-our-endpoint",
        "placeholder-host",
        "disabled",
        "two-stores",
    ],
)
async def test_a_template_that_is_not_this_store_is_unavailable(
    rows, monkeypatch, servers
):
    seen = _store(monkeypatch, {ORDER_PATH: httpx.Response(200, json=ORDER)})
    with pytest.raises(woo_ot.OrderLookupUnavailable):
        await _lookup(phone="9876543210", context=_context(servers=servers))
    assert seen == []


@pytest.mark.parametrize(
    "url",
    [TOOL_SERVER, "https://api.breezebuddy.ai/mcp/woocommerce/WWW.ShopyVision.com/"],
)
async def test_the_store_is_the_tool_server_host(rows, monkeypatch, url):
    seen = _store(monkeypatch, {ORDER_PATH: httpx.Response(200, json=ORDER)})
    context = _context(servers=[{"url": url}])
    status, _ = await _lookup(phone="9876543210", context=context)
    assert status == 200 and seen[0].url.host == HOST


async def test_an_oversized_store_answer_is_unavailable(rows, monkeypatch):
    async def too_large(*args, **kwargs):
        raise store_api.ResponseTooLarge("more than 2000000 bytes")

    monkeypatch.setattr(store_api, "get", too_large)
    with pytest.raises(woo_ot.OrderLookupUnavailable):
        await _lookup(phone="9876543210")


async def test_store_refusal_and_transport_errors_are_unavailable(rows, monkeypatch):
    _store(monkeypatch, {ORDER_PATH: httpx.Response(401, json={"code": "x"})})
    with pytest.raises(woo_ot.OrderLookupUnavailable):
        await _lookup(phone="9876543210")

    def boom(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(
        store_api,
        "create_http_client",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(boom)),
    )
    with pytest.raises(woo_ot.OrderLookupUnavailable):
        await _lookup(phone="9876543210")


async def test_a_redirecting_store_is_unavailable_and_named(rows, monkeypatch):
    seen = _store(
        monkeypatch,
        {
            ORDER_PATH: httpx.Response(
                301, headers={"location": "https://shopyvision.com/wp-json/"}
            )
        },
    )
    with pytest.raises(woo_ot.OrderLookupUnavailable, match="redirects to https://"):
        await _lookup(phone="9876543210")
    assert len(seen) == 1


async def test_get_order_status_end_to_end(rows, monkeypatch):
    _store(
        monkeypatch,
        {
            ORDER_PATH: httpx.Response(200, json=ORDER),
            TRACKING_PATH: httpx.Response(200, json=TRACKINGS),
        },
    )
    context = _context()
    result = await handlers.get_order_status(
        context, {"orderNumber": "#4512", "phone": "98765 43210"}
    )
    assert result["status"] == "success"
    assert result["data"]["orders"][0]["order_name"] == "#4512"
    assert context.bot.agent_state["tracking_urls"] == [TRACKINGS[0]["tracking_link"]]


async def test_a_template_naming_no_credential_never_reads_one(rows, monkeypatch):
    async def no_read(*args, **kwargs):
        raise AssertionError("a credential was read with none named")

    monkeypatch.setattr(woo_ot, "get_credential_by_id", no_read)
    seen = _store(monkeypatch, {ORDER_PATH: httpx.Response(200, json=ORDER)})
    with pytest.raises(woo_ot.OrderLookupUnavailable, match="flavor.ucp.credentials"):
        await _lookup(phone="9876543210", context=_context(credential_id=None))
    assert seen == []


async def test_a_bad_key_is_named_by_field_never_by_value(rows, monkeypatch):
    rows[:] = [_row(consumer_secret="", extra="s3cret-value")]
    with pytest.raises(woo_ot.OrderLookupUnavailable) as raised:
        await _lookup(phone="9876543210")
    assert "consumer_secret" in str(raised.value) and "extra" in str(raised.value)
    assert "s3cret-value" not in str(raised.value)


# ===========================================================================
# The builtins: get_order_status, then read_page_content
# ===========================================================================

TRACKING_URL = "https://www.bluedart.com/track?trackNo=77983312643"
CARD_ORDER = {
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


def _tool_context(tracking_urls: Any = None) -> Any:
    configurations = ConfigurationModel.model_validate(
        {"ui_catalog": {"enabled_groups": ["core", "commerce"]}}
    )
    template = SimpleNamespace(
        id="tpl-1", merchant_id="shopyvision", configurations=configurations
    )
    bot = SimpleNamespace(template=template, template_vars={}, agent_state={})
    if tracking_urls is not None:
        bot.agent_state["tracking_urls"] = list(tracking_urls)
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

    monkeypatch.setattr(handlers, "lookup_order", fake)
    return calls


# ---------------------------------------------------------------------------
# Registration: a template declares the two builtins itself
# ---------------------------------------------------------------------------


def test_the_declared_entries_reach_their_handlers():
    expected = {
        "woocommerce_order_status": handlers.get_order_status,
        "read_tracking_page": wismo.read_page_content,
    }
    for entry in TEMPLATE_FUNCTIONS:
        built = GlobalBuiltinFunction.model_validate(entry)
        assert BUILTIN_HANDLERS[built.handler] is expected[built.handler]


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
    calls = _lookup_answers(monkeypatch, (200, {"found": True, "orders": [CARD_ORDER]}))
    context = _tool_context()
    result = await handlers.get_order_status(
        context, {"orderNumber": " #4512 ", "phone": "+91 98765-43210"}
    )
    assert calls == [{"order_number": "4512", "phone": "919876543210", "email": None}]
    assert result["status"] == "success"
    assert result["data"]["orders"] == [CARD_ORDER]
    assert context.bot.agent_state["tracking_urls"] == [TRACKING_URL]
    # Chat gives the model only ``data`` of a success, so ``next`` lives there.
    assert "read_page_content" in normalize("get_order_status", result)["next"]


@pytest.mark.parametrize("url", [None, "http://www.bluedart.com/track?n=1"])
async def test_no_readable_tracking_url_means_no_page_read_step(monkeypatch, url):
    # read_page_content reads https links only, so naming it would only fail.
    order = {**CARD_ORDER, "tracking_url": url}
    _lookup_answers(monkeypatch, (200, {"found": True, "orders": [order]}))
    context = _tool_context()
    result = await handlers.get_order_status(
        context, {"orderNumber": "4512", "email": "a@b.c"}
    )
    assert result["data"]["next"] == handlers.NEXT_RENDER
    assert "tracking_urls" not in context.bot.agent_state  # nothing to read


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
    calls = _lookup_answers(monkeypatch, (200, {"found": True, "orders": [CARD_ORDER]}))
    result = await handlers.get_order_status(_tool_context(), args)
    assert result["error"] == "missing_identifier"
    assert calls == []


async def test_a_short_phone_beside_an_email_is_looked_up(monkeypatch):
    # The lookup ignores a phone under 10 digits, so the email decides.
    calls = _lookup_answers(monkeypatch, (200, {"found": True, "orders": [CARD_ORDER]}))
    result = await handlers.get_order_status(
        _tool_context(), {"orderNumber": "4512", "phone": "43210", "email": "a@b.c"}
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
    result = await handlers.get_order_status(
        _tool_context(), {"orderNumber": "4512", "phone": "9876543210"}
    )
    assert result == {
        "status": "error",
        "error": "order_not_matched",
        "message": handlers.NOT_MATCHED,
        "next": handlers.NEXT_FIX_ARGS,
    }


async def test_an_unreachable_store_is_unavailable(monkeypatch):
    _lookup_answers(monkeypatch, OrderLookupUnavailable("store answered 401"))
    result = await handlers.get_order_status(
        _tool_context(), {"orderNumber": "4512", "phone": "9876543210"}
    )
    assert result["error"] == "wismo_not_available"
    assert result["message"] == handlers.NOT_REACHABLE


async def test_a_failed_lookup_keeps_the_links_already_saved(monkeypatch):
    _lookup_answers(monkeypatch, (404, {"found": False, "error": "order_not_found"}))
    context = _tool_context(tracking_urls=["https://t.example/previous"])
    await handlers.get_order_status(
        context, {"orderNumber": "2", "phone": "9876543210"}
    )
    assert context.bot.agent_state["tracking_urls"] == ["https://t.example/previous"]


def test_saved_links_are_a_capped_list_chat_persists():
    context = _tool_context()
    for n in range(7):
        assert wismo.save_tracking_url(context, f"https://t.example/{n}")
    assert wismo.save_tracking_url(context, "https://t.example/3")  # moves last
    urls = context.bot.agent_state["tracking_urls"]
    assert urls == [f"https://t.example/{n}" for n in (2, 4, 5, 6, 3)]
    assert len(urls) == wismo.TRACKING_URLS_KEPT
    # Chat saves only changed keys: the list is replaced, so it is one of them.
    before = {"tracking_urls": urls[:-1]}
    assert diff_state_patch(before, context.bot.agent_state) == {"tracking_urls": urls}


@pytest.mark.parametrize("url", ["http://x.example/t", "javascript:alert(1)", "", None])
def test_only_https_links_are_saved(url):
    context = _tool_context()
    assert not wismo.save_tracking_url(context, url)
    assert "tracking_urls" not in context.bot.agent_state


def test_results_use_the_http_tool_envelope():
    from app.ai.voice.agents.breeze_buddy.template.session_state import (
        _is_tool_success,
        _unwrap_tool_payload,
    )

    ok = {"status": "success", "status_code": 200, "data": {"found": True}}
    bad = wismo.tool_error("order_not_found", "no")
    assert _is_tool_success(ok) and not _is_tool_success(bad)
    assert _unwrap_tool_payload(ok) == {"found": True}
    assert json.dumps(ok)


# ---------------------------------------------------------------------------
# read_page_content (commerce/ucp/wismo.py)
# ---------------------------------------------------------------------------


def _mock_reader(monkeypatch, handler) -> List[httpx.Request]:
    seen: List[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        wismo,
        "create_http_client",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    return seen


OTHER_URL = "https://www.delhivery.com/track/package/1234567890"


async def test_reads_the_saved_link_the_model_names(monkeypatch):
    monkeypatch.setattr(wismo, "WISMO_PAGE_READER_URL", "https://reader.test/{url}")
    seen = _mock_reader(
        monkeypatch, lambda r: httpx.Response(200, text="Shipment Delivered 08 Oct")
    )
    # Two lookups saved two links; each page read names its own.
    context = _tool_context(tracking_urls=[OTHER_URL, TRACKING_URL])
    result = await wismo.read_page_content(context, {"url": TRACKING_URL})
    assert seen[0].url == httpx.URL(f"https://reader.test/{TRACKING_URL}")
    assert seen[0].headers["X-Engine"] == "browser"
    await wismo.read_page_content(context, {"url": OTHER_URL})
    assert seen[1].url == httpx.URL(f"https://reader.test/{OTHER_URL}")
    assert result["data"]["page_text"] == "Shipment Delivered 08 Oct"
    assert normalize("read_page_content", result)["next"] == wismo.NEXT_TRANSCRIBE


@pytest.mark.parametrize(
    "saved, url",
    [
        ([TRACKING_URL], "https://evil.example/x"),
        ([TRACKING_URL], TRACKING_URL + "&x=1"),
        ([], TRACKING_URL),
        (None, TRACKING_URL),
        ([TRACKING_URL], ""),
        ([TRACKING_URL], None),
    ],
    ids=["other-link", "changed-link", "nothing-saved", "no-key", "empty", "none"],
)
async def test_refuses_any_link_no_lookup_saved(monkeypatch, saved, url):
    seen = _mock_reader(monkeypatch, lambda r: httpx.Response(200))
    result = await wismo.read_page_content(
        _tool_context(tracking_urls=saved), {"url": url}
    )
    assert result["error"] == "invalid_url" and "get_order_status" in result["next"]
    assert seen == []


async def test_an_unreadable_page_keeps_the_first_card(monkeypatch):
    _mock_reader(monkeypatch, lambda r: httpx.Response(422, text="refused"))
    result = await wismo.read_page_content(
        _tool_context(tracking_urls=[TRACKING_URL]), {"url": TRACKING_URL}
    )
    assert result["error"] == "page_not_readable"
    assert "first card stands" in result["next"]
