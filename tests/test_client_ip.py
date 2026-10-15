"""client_ip: the caller address rate limits are keyed on.

Shapes are taken from the production router's access log, where the GCP load
balancer leaves ``X-Forwarded-For`` as ``<client>, 34.111.84.202`` and the
router may append a Google front-end (35.191.x) or in-cluster (10.x) hop.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request

from app.api.routers.breeze_buddy.widget_common import client_ip

LB = "34.111.84.202"


def _req(xff: str | None, peer: str | None = "10.196.3.7") -> Request:
    headers = {"x-forwarded-for": xff} if xff is not None else {}
    client = SimpleNamespace(host=peer) if peer else None
    return cast(Request, SimpleNamespace(headers=headers, client=client))


@pytest.mark.parametrize(
    "xff",
    [
        f"35.154.93.248, {LB}",  # as the router receives it
        f"35.154.93.248, {LB}, 35.191.24.216",  # router appended a front end
        f"35.154.93.248, {LB}, 10.196.3.7",  # router appended an in-cluster hop
        f"35.154.93.248, {LB}, 130.211.0.9, 10.196.3.7",
        f"35.154.93.248 , {LB} ,",  # stray spaces and a trailing comma
    ],
)
def test_the_client_is_the_entry_before_the_load_balancer(xff):
    assert client_ip(_req(xff)) == "35.154.93.248"


def test_client_supplied_entries_cannot_choose_the_bucket():
    spoofed = _req(f"6.6.6.6, 7.7.7.7, 35.154.93.248, {LB}, 35.191.24.216")
    assert client_ip(spoofed) == "35.154.93.248"


def test_every_caller_does_not_share_the_load_balancer_bucket():
    a = client_ip(_req(f"13.126.232.13, {LB}, 35.191.70.183"))
    b = client_ip(_req(f"35.154.93.248, {LB}, 35.191.24.233"))
    assert {a, b} == {"13.126.232.13", "35.154.93.248"}


def test_a_single_entry_is_used_as_is():
    # Not through the load balancer (local / direct): nothing to skip.
    assert client_ip(_req("203.0.113.5")) == "203.0.113.5"


def test_no_header_falls_back_to_the_peer_then_unknown():
    assert client_ip(_req(None, peer="203.0.113.9")) == "203.0.113.9"
    assert client_ip(_req(None, peer=None)) == "unknown"
    assert client_ip(_req("", peer=None)) == "unknown"


def test_trusted_proxies_come_from_config(monkeypatch):
    import ipaddress

    from app.api.routers.breeze_buddy import widget_common

    xff = f"35.154.93.248, {LB}, 35.191.24.216"
    monkeypatch.setattr(
        widget_common,
        "_TRUSTED_PROXY_NETWORKS",
        (ipaddress.ip_network("10.0.0.0/8"),),
    )
    # 35.191.x no longer trusted: it is taken as the load balancer's entry, so
    # the real load balancer address is (wrongly) read as the caller.
    assert client_ip(_req(xff)) == LB


def test_default_config_trusts_only_infrastructure():
    from app.core.config.static import TRUSTED_PROXY_CIDRS

    assert TRUSTED_PROXY_CIDRS.split(",") == [
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "35.191.0.0/16",
        "130.211.0.0/22",
    ]
