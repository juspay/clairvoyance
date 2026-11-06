"""A global HTTP function's URL host may not come from the LLM (PT-17).

A template author writing ``https://api.shop.com/orders/{order_id}`` is giving
the model a path segment. One writing ``{base_url}/orders`` is giving it the
destination, and prompt injection then chooses where the platform sends a
credentialed request. The host is refused; path and query are not.

The parsing is the subtle part. ``urlparse`` only fills ``netloc`` when the
string contains ``//``, so ``{base_url}/orders`` parses with an EMPTY netloc —
a netloc-based check sees no host at all and waves it through.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Dict, Optional, Tuple, cast

import pytest

import app.ai.voice.agents.breeze_buddy.handlers.transport.http_handler as handler_mod
from app.ai.voice.agents.breeze_buddy.handlers.transport.http_handler import (
    _authority_region,
    http_function_handler,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import (
    FieldConfig,
    FieldSource,
    GlobalHttpFunction,
    HttpMethod,
    HttpRequestConfig,
)

REFUSAL = "LLM-sourced field cannot"


# --- the authority region ---------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        # No "://" at all: everything before the first separator is the host.
        ("{base_url}/orders", "{base_url}"),
        ("{base_url}", "{base_url}"),
        # A placeholder spanning part of the host.
        ("https://{sub}.shop.com/o", "{sub}.shop.com"),
        # Placeholders below the host do not belong to the authority.
        ("https://api.shop.com/orders/{order_id}", "api.shop.com"),
        ("https://api.shop.com/o?id={order_id}", "api.shop.com"),
        ("https://api.shop.com/o#{frag}", "api.shop.com"),
        ("https://api.shop.com", "api.shop.com"),
        ("", ""),
    ],
)
def test_authority_region_isolates_scheme_and_host(url, expected):
    assert _authority_region(url) == expected


def test_authority_region_sees_a_host_where_urlparse_sees_none():
    """The bypass this function exists to close."""
    from urllib.parse import urlparse

    assert urlparse("{base_url}/orders").netloc == ""  # a netloc check misses it
    assert _authority_region("{base_url}/orders") == "{base_url}"


# --- the handler ------------------------------------------------------------


def _fn(url: str, fields: Dict[str, FieldConfig]) -> GlobalHttpFunction:
    return GlobalHttpFunction(
        name="check_order",
        description="check an order",
        expected_fields=fields,
        http_request=HttpRequestConfig(url=url, method=HttpMethod.GET),
    )


def _ctx() -> TemplateContext:
    # The host check runs before any field resolution or network use; only the
    # session's presence is asserted before it.
    return cast(TemplateContext, SimpleNamespace(aiohttp_session=object()))


@pytest.fixture
def no_network(monkeypatch):
    """Stop the allowed cases at the point the check would have let them past."""
    calls: list = []

    class _Executor:
        def __init__(self, session):
            pass

        async def execute(self, *a, **kw) -> Optional[Tuple[int, str]]:
            calls.append(kw or a)
            return (200, "{}")

    monkeypatch.setattr(handler_mod, "HttpRequestExecutor", _Executor)
    return calls


@pytest.mark.parametrize(
    "url",
    ["{base_url}/orders", "https://{base_url}.shop.com/orders", "{base_url}"],
)
async def test_an_llm_field_in_the_host_is_refused(url, no_network):
    result, _ = await http_function_handler(
        _ctx(),
        {"base_url": "https://attacker.example"},
        _fn(url, {"base_url": FieldConfig(source=FieldSource.LLM, value="base_url")}),
    )

    assert result["status"] == "error"
    assert REFUSAL in result["error"]
    assert no_network == [], "refused, so nothing should have been sent"


@pytest.mark.parametrize(
    "url",
    [
        "https://api.shop.com/orders/{order_id}",
        "https://api.shop.com/orders?id={order_id}",
    ],
)
async def test_an_llm_field_below_the_host_is_allowed(url, no_network):
    result, _ = await http_function_handler(
        _ctx(),
        {"order_id": "KP-99"},
        _fn(url, {"order_id": FieldConfig(source=FieldSource.LLM, value="order_id")}),
    )

    assert REFUSAL not in str(result.get("error", ""))


async def test_a_non_llm_field_in_the_host_is_allowed(no_network):
    """A shop domain from the lead payload is the tenant's own configuration.

    Only the LLM is untrusted here; refusing STATIC fields would break every
    template addressing a per-merchant host.
    """
    result, _ = await http_function_handler(
        _ctx(),
        {},
        _fn(
            "https://{shop}.myshopify.com/orders",
            {"shop": FieldConfig(source=FieldSource.STATIC, value="acme")},
        ),
    )

    assert REFUSAL not in str(result.get("error", ""))


async def test_the_check_matches_on_the_argument_name_too(no_network):
    """expected_fields may key a field under one name and read another.

    Matching only the dict key would let ``{shop_host}`` through when the field
    is keyed ``host`` and reads the LLM argument ``shop_host``.
    """
    result, _ = await http_function_handler(
        _ctx(),
        {"shop_host": "attacker.example"},
        _fn(
            "https://{shop_host}/orders",
            {"host": FieldConfig(source=FieldSource.LLM, value="shop_host")},
        ),
    )

    assert result["status"] == "error"
    assert REFUSAL in result["error"]
