"""``POST /assist/probe``: who may ask, how often, and what comes back.

The engine's own guard is covered in ``test_fetch_guard``; what matters here
is that the route refuses before it fetches — an unauthorised or rate-limited
caller must never cause an outbound request.
"""

from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ai.voice.agents.breeze_buddy.assist.engine.probe import profile_from_page
from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingConfigurationError,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    EgressNotGuardedError,
    FetchFailedError,
    UnsafeUrlError,
)
from app.api.routers.breeze_buddy.assist import probe
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo, UserRole
from app.services.redis.rate_limit import RateLimitDecision

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "probe"

ADMIN = UserInfo(
    id="admin-1", username="admin", role=UserRole.ADMIN, reseller_ids=["*"]
)
PARTNER = UserInfo(
    id="r-1", username="partner", role=UserRole.RESELLER, reseller_ids=["BB_SHOPIFY"]
)
MERCHANT = UserInfo(
    id="m-1",
    username="merchant",
    role=UserRole.MERCHANT,
    reseller_ids=["BB_SHOPIFY"],
    merchant_ids=["9b1086-18.myshopify.com"],
)


def _hosted_profile():
    return profile_from_page(
        url="https://hustleculture.co.in/",
        final_url="https://hustleculture.co.in/",
        status=200,
        headers={"powered-by": "Shopify"},
        cookie_names=["_shopify_y", "_shopify_s"],
        body=(FIXTURES / "shopify_home.html").read_text(),
    )


@pytest.fixture()
def probe_site(monkeypatch) -> AsyncMock:
    mock = AsyncMock(return_value=_hosted_profile())
    monkeypatch.setattr(probe, "probe_site", mock)
    monkeypatch.setattr(
        probe,
        "check_rate_limit",
        AsyncMock(
            return_value=RateLimitDecision(
                allowed=True, count=1, limit=120, retry_after_seconds=0
            )
        ),
    )
    return mock


def _client(user: UserInfo) -> TestClient:
    app = FastAPI()
    app.include_router(probe.router)
    app.dependency_overrides[get_current_user_with_rbac] = lambda: user
    return TestClient(app)


def _post(user: UserInfo, **body):
    payload = {"url": "https://hustleculture.co.in/", "reseller_id": "BB_SHOPIFY"}
    payload.update(body)
    return _client(user).post("/assist/probe", json=payload)


def test_the_report_names_the_platform_and_shows_its_evidence(probe_site) -> None:
    response = _post(ADMIN)
    assert response.status_code == 200
    body = response.json()

    assert body["platform"] == "shopify"
    assert body["confidence"] > 0
    assert body["identity"]["canonical_host"] == "hustleculture.co.in"
    assert body["identity"]["permanent_host"] == "9b1086-18.myshopify.com"
    assert body["site"]["challenge"] is False
    assert body["site"]["truncated"] is False
    assert body["site"]["size_bytes"] > 0
    assert body["site"]["fetched_at"]
    assert body["site"]["script_hosts"][0]["scripts"] >= 1
    assert body["site"]["structured_data"]
    assert body["scores"]["generic"] == 0.0
    assert body["matched_signals"], "a verdict with no evidence is not reviewable"


def test_a_partner_may_probe_inside_its_own_reseller(probe_site) -> None:
    assert _post(PARTNER).status_code == 200


def test_a_partner_may_not_probe_for_another_reseller(probe_site) -> None:
    response = _post(PARTNER, reseller_id="BB_ASSIST")
    assert response.status_code == 403
    probe_site.assert_not_awaited()


def test_a_merchant_may_probe(probe_site) -> None:
    assert _post(MERCHANT, merchant_id="9b1086-18.myshopify.com").status_code == 200
    probe_site.assert_awaited_once()


@pytest.mark.parametrize("merchant_id", [None, "", "someone-else.myshopify.com"])
def test_a_merchant_must_name_a_merchant_in_its_scope(probe_site, merchant_id) -> None:
    body = {} if merchant_id is None else {"merchant_id": merchant_id}
    assert _post(MERCHANT, **body).status_code == 403
    probe_site.assert_not_awaited()


def test_an_unsafe_url_is_a_400(probe_site) -> None:
    probe_site.side_effect = UnsafeUrlError("url must use a public host")
    response = _post(ADMIN, url="https://169.254.169.254/")
    assert response.status_code == 400
    assert "public host" in response.json()["detail"]


def test_a_deployment_that_cannot_probe_safely_answers_503(probe_site) -> None:
    # Not the caller's fault, so not a 4xx: the feature is off here.
    probe_site.side_effect = EgressNotGuardedError("egress is proxied")
    response = _post(ADMIN)
    assert response.status_code == 503


def test_a_site_that_cannot_be_read_is_a_400(probe_site) -> None:
    probe_site.side_effect = FetchFailedError("timed out reading the site")
    response = _post(ADMIN)
    assert response.status_code == 400
    assert "timed out" in response.json()["detail"]


def test_over_the_cap_nothing_is_fetched(monkeypatch, probe_site) -> None:
    monkeypatch.setattr(
        probe,
        "check_rate_limit",
        AsyncMock(
            return_value=RateLimitDecision(
                allowed=False, count=121, limit=120, retry_after_seconds=42
            )
        ),
    )
    response = _post(ADMIN)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "42"
    probe_site.assert_not_awaited()


# What Shopify sends a data-centre address: a short page, status 429.
_TURNED_AWAY = (
    "<html><head><title>Too many requests</title></head>"
    "<body><h1>Too many requests</h1></body></html>"
)


def _blocked_profile():
    return profile_from_page(
        url="https://hustleculture.co.in/",
        final_url="https://hustleculture.co.in/",
        status=429,
        headers={},
        cookie_names=[],
        body=_TURNED_AWAY,
    )


def test_a_blocked_probe_is_read_again_through_firecrawl(
    monkeypatch, probe_site
) -> None:
    probe_site.return_value = _blocked_profile()
    read_html = AsyncMock(
        return_value=(
            "https://hustleculture.co.in/",
            200,
            (FIXTURES / "shopify_home.html").read_text(),
        )
    )
    monkeypatch.setattr(probe.firecrawl, "read_html", read_html)
    body = _post(ADMIN).json()
    read_html.assert_awaited_once()
    assert body["platform"] == "shopify"
    assert body["identity"]["permanent_host"] == "9b1086-18.myshopify.com"
    assert body["site"]["challenge"] is False
    assert body["site"]["name"] != "Too many requests"


def test_a_render_of_the_rate_limit_page_is_still_blocked(
    monkeypatch, probe_site
) -> None:
    # Firecrawl may report the platform's rate-limit page as status 200.
    probe_site.return_value = _blocked_profile()
    monkeypatch.setattr(
        probe.firecrawl,
        "read_html",
        AsyncMock(return_value=("https://hustleculture.co.in/", 200, _TURNED_AWAY)),
    )
    body = _post(ADMIN).json()
    assert body["site"]["challenge"] is True
    assert body["site"]["name"] is None


def test_a_probe_that_stays_blocked_names_no_store(monkeypatch, probe_site) -> None:
    probe_site.return_value = _blocked_profile()
    monkeypatch.setattr(
        probe.firecrawl,
        "read_html",
        AsyncMock(side_effect=WebsiteScrapingConfigurationError("no key")),
    )
    body = _post(ADMIN).json()
    assert body["site"]["challenge"] is True
    # The interstitial's title is not the store's name: the console shows
    # the address instead.
    assert body["site"]["name"] is None
    assert body["site"]["title"] is None


def test_an_open_store_is_not_rendered_again(monkeypatch, probe_site) -> None:
    read_html = AsyncMock()
    monkeypatch.setattr(probe.firecrawl, "read_html", read_html)
    assert _post(ADMIN).json()["platform"] == "shopify"
    read_html.assert_not_awaited()
