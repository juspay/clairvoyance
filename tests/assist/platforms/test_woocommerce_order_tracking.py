"""WooCommerce order lookup on the ``order_lookup`` seam.

The store comes from the template's ``/mcp/woocommerce/<host>`` tool server
URL, else its ``secrets.shop_url``; the key from the merchant's one
``provider="woocommerce"`` credential row.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import pytest

import app.ai.voice.agents.breeze_buddy.assist.commerce  # noqa: F401 — registers the flavor
from app.ai.voice.agents.breeze_buddy.accounts import (
    AccountShapeError,
    WooCommerceAccount,
    account_from_value,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp import (
    order_tracking as commerce_ot,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.hooks import (
    OrderLookupUnavailable,
    resolve_order_lookup,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce import (
    order_tracking as woo_ot,
    store_api,
)
from app.ai.voice.agents.breeze_buddy.template.types import ConfigurationModel

HOST = "www.shopyvision.com"
TOOL_SERVER = f"https://api.breezebuddy.ai/mcp/woocommerce/{HOST}"
MERCHANT = "www.shopyvision.com"
ACCOUNT = {
    "consumer_key": "ck_test",
    "consumer_secret": "cs_test",
    "endpoint": f"https://{HOST}",
}
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


def _configurations(connectors: Any = ("woocommerce",), servers: Any = None):
    if servers is None:
        servers = [{"url": TOOL_SERVER}]
    return ConfigurationModel.model_validate(
        {
            "mcp": {"servers": servers},
            "ui_catalog": {"enabled_groups": ["core", "commerce"]},
            "flavor": {
                "ucp": {
                    "connectors": list(connectors),
                    "features": {"order_tracking": True},
                }
            },
        }
    )


def _context(
    connectors: Any = ("woocommerce",), servers: Any = None, secrets: Any = None
) -> Any:
    configurations = _configurations(connectors, servers)
    template = SimpleNamespace(
        reseller_id="BB_ASSIST",
        merchant_id=MERCHANT,
        configurations=configurations,
        secrets=secrets,
    )
    bot = SimpleNamespace(template=template, template_vars={}, agent_state={})
    return SimpleNamespace(bot=bot, call_sid="c1", configurations=configurations)


def _row(merchant_id: Any = MERCHANT, **value: Any) -> Any:
    return SimpleNamespace(merchant_id=merchant_id, value={**ACCOUNT, **value})


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
    found: List[Any] = [_row()]

    async def fake_rows(reseller_id, mask, merchant_id, provider):
        assert (reseller_id, mask, merchant_id, provider) == (
            "BB_ASSIST",
            False,
            MERCHANT,
            "woocommerce",
        )
        return found

    monkeypatch.setattr(woo_ot, "get_credentials_by_merchant", fake_rows)
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


def test_the_connector_seam_knows_woocommerce():
    assert resolve_order_lookup(["woocommerce"]) == ("woocommerce", woo_ot.lookup_order)


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
        ("partial-shipped", "partial"),
        ("cancelled", "cancelled"),
    ],
)
async def test_status_mapping(rows, monkeypatch, woo_status, fulfillment):
    _store(
        monkeypatch,
        {ORDER_PATH: httpx.Response(200, json={**ORDER, "status": woo_status})},
    )
    _, body = await _lookup(phone="9876543210")
    assert body["orders"][0]["fulfillment_status"] == fulfillment


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


async def test_identity_rules_match_nautilus(rows, monkeypatch):
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
    order = {**ORDER, "shipping": {"phone": "070698 60258"}}
    _store(monkeypatch, {ORDER_PATH: httpx.Response(200, json=order)})
    assert (await _lookup(phone="7069860258"))[0] == 200
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


async def test_a_missing_orders_route_is_unavailable_not_not_found(rows, monkeypatch):
    # A 404 without WooCommerce's "no such order" code: the REST API is off
    # or the URL is wrong. Saying "order not found" would mislead the shopper.
    _store(monkeypatch, {ORDER_PATH: httpx.Response(404, json=NO_ROUTE)})
    with pytest.raises(OrderLookupUnavailable):
        await _lookup(phone="9876543210")


async def test_a_malformed_billing_never_matches(rows, monkeypatch):
    _store(
        monkeypatch,
        {ORDER_PATH: httpx.Response(200, json={**ORDER, "billing": ["x"]})},
    )
    assert (await _lookup(phone="9876543210"))[1]["error"] == "identity_mismatch"


async def test_non_ascii_digits_are_not_found_without_a_call(rows, monkeypatch):
    seen = _store(monkeypatch, {ORDER_PATH: httpx.Response(200, json=ORDER)})
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
    with pytest.raises(OrderLookupUnavailable, match="did not answer"):
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


def test_not_found_answers_are_not_shared():
    first = woo_ot._not_found()
    first[1]["error"] = "changed"
    assert woo_ot._not_found()[1]["error"] == "order_not_found"


@pytest.mark.parametrize(
    "found",
    [
        [],
        [_row(), _row()],
        [_row(merchant_id=None)],
        [_row(endpoint="https://another-store.com")],
        [_row(endpoint="http://www.shopyvision.com")],
        [_row(consumer_secret="")],
    ],
    ids=["none", "two", "reseller-wide", "other-host", "plain-http", "no-secret"],
)
async def test_an_unusable_account_never_calls_the_store(rows, monkeypatch, found):
    rows[:] = found
    seen = _store(monkeypatch, {ORDER_PATH: httpx.Response(200, json=ORDER)})
    with pytest.raises(OrderLookupUnavailable):
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
        [{"url": "https://api.breezebuddy.ai/mcp/woocommerce/x.com"}],
    ],
    ids=[
        "no-tool-server",
        "not-our-endpoint",
        "placeholder-host",
        "disabled",
        "two-stores",
        "account-for-another-host",
    ],
)
async def test_a_template_that_is_not_this_store_is_unavailable(
    rows, monkeypatch, servers
):
    seen = _store(monkeypatch, {ORDER_PATH: httpx.Response(200, json=ORDER)})
    with pytest.raises(OrderLookupUnavailable):
        await _lookup(phone="9876543210", context=_context(servers=servers))
    assert seen == []


@pytest.mark.parametrize(
    "url",
    [TOOL_SERVER, "https://api.breezebuddy.ai/mcp/woocommerce/WWW.ShopyVision.com/"],
)
async def test_the_store_is_the_tool_server_host(rows, monkeypatch, url):
    seen = _store(monkeypatch, {ORDER_PATH: httpx.Response(200, json=ORDER)})
    # The tool server wins over secrets.shop_url.
    context = _context(servers=[{"url": url}], secrets={"shop_url": "x.com"})
    status, _ = await _lookup(phone="9876543210", context=context)
    assert status == 200 and seen[0].url.host == HOST


@pytest.mark.parametrize(
    "servers",
    [[], [{"url": "https://api.breezebuddy.ai/mcp/woocommerce/{shop_url}"}]],
    ids=["no-tool-server", "placeholder-host"],
)
@pytest.mark.parametrize("shop_url", [HOST, f"https://{HOST}/", "WWW.ShopyVision.com"])
async def test_without_a_tool_server_the_store_is_shop_url(
    rows, monkeypatch, servers, shop_url
):
    seen = _store(monkeypatch, {ORDER_PATH: httpx.Response(200, json=ORDER)})
    context = _context(servers=servers, secrets={"shop_url": shop_url})
    status, _ = await _lookup(phone="9876543210", context=context)
    assert status == 200 and seen[0].url.host == HOST


async def test_two_stores_never_fall_back_to_shop_url(rows, monkeypatch):
    seen = _store(monkeypatch, {ORDER_PATH: httpx.Response(200, json=ORDER)})
    servers = [
        {"url": TOOL_SERVER},
        {"url": "https://api.breezebuddy.ai/mcp/woocommerce/x.com"},
    ]
    context = _context(servers=servers, secrets={"shop_url": HOST})
    with pytest.raises(OrderLookupUnavailable):
        await _lookup(phone="9876543210", context=context)
    assert seen == []


async def test_an_oversized_store_answer_is_unavailable(rows, monkeypatch):
    async def too_large(*args, **kwargs):
        raise store_api.ResponseTooLarge("more than 2000000 bytes")

    monkeypatch.setattr(store_api, "get", too_large)
    with pytest.raises(OrderLookupUnavailable):
        await _lookup(phone="9876543210")


async def test_store_refusal_and_transport_errors_are_unavailable(rows, monkeypatch):
    _store(monkeypatch, {ORDER_PATH: httpx.Response(401, json={"code": "x"})})
    with pytest.raises(OrderLookupUnavailable):
        await _lookup(phone="9876543210")

    def boom(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(
        store_api,
        "create_http_client",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(boom)),
    )
    with pytest.raises(OrderLookupUnavailable):
        await _lookup(phone="9876543210")


async def test_get_order_status_end_to_end(rows, monkeypatch):
    _store(
        monkeypatch,
        {
            ORDER_PATH: httpx.Response(200, json=ORDER),
            TRACKING_PATH: httpx.Response(200, json=TRACKINGS),
        },
    )
    context = _context()
    result = await commerce_ot.get_order_status(
        context, {"orderNumber": "#4512", "phone": "98765 43210"}
    )
    assert result["status"] == "success"
    assert result["data"]["orders"][0]["order_name"] == "#4512"
    assert context.bot.agent_state["tracking_url"] == TRACKINGS[0]["tracking_link"]


def test_the_account_shape_is_judged_at_the_write():
    account = account_from_value("woocommerce", ACCOUNT)
    assert isinstance(account, WooCommerceAccount)
    for bad in (
        {**ACCOUNT, "endpoint": "http://x.com"},
        {k: v for k, v in ACCOUNT.items() if k != "endpoint"},
        {**ACCOUNT, "consumer_key": " "},
        {**ACCOUNT, "consumer_secret": ""},
        {**ACCOUNT, "extra": "x"},
    ):
        with pytest.raises(AccountShapeError):
            account_from_value("woocommerce", bad)
