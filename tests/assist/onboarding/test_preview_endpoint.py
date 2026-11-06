"""``POST /assist/onboarding/preview``: who may ask and what comes back. The probe and the
brand lane are replaced, so nothing is fetched."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ai.voice.agents.breeze_buddy.assist.engine.models import SiteProfile
from app.ai.voice.agents.breeze_buddy.assist.engine.research.providers.firecrawl import (
    Branding,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    EgressNotGuardedError,
    UnsafeUrlError,
)
from app.api.routers.breeze_buddy.assist.onboarding import preview
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo, UserRole
from app.services.redis.rate_limit import RateLimitDecision

ADMIN = UserInfo(
    id="admin-1", username="admin", role=UserRole.ADMIN, reseller_ids=["*"]
)
MERCHANT = UserInfo(
    id="m-1",
    username="merchant",
    role=UserRole.MERCHANT,
    reseller_ids=["BB_SHOPIFY"],
    merchant_ids=["9b1086-18.myshopify.com"],
)
PROFILE = SiteProfile(
    url="https://store.example/", final_url="https://store.example/", status=200
)
LOOK = Branding(
    primary_color="#c22126",
    logo_url="https://cdn.example/logo.png",
)


@pytest.fixture()
def probe(monkeypatch) -> AsyncMock:
    probe_site = AsyncMock(return_value=PROFILE)
    monkeypatch.setattr(preview, "probe_site", probe_site)
    monkeypatch.setattr(preview, "read_branding", AsyncMock(return_value=LOOK))
    monkeypatch.setattr(
        preview,
        "check_rate_limit",
        AsyncMock(
            return_value=RateLimitDecision(
                allowed=True, count=1, limit=10, retry_after_seconds=0
            )
        ),
    )
    return probe_site


def _post(user: UserInfo, **body):
    app = FastAPI()
    app.include_router(preview.router)
    app.dependency_overrides[get_current_user_with_rbac] = lambda: user
    payload = {"url": "https://store.example/", "reseller_id": "BB_SHOPIFY", **body}
    return TestClient(app).post("/assist/onboarding/preview", json=payload)


def test_returns_the_colours_and_logo(probe) -> None:
    response = _post(MERCHANT, merchant_id="9b1086-18.myshopify.com")
    assert response.status_code == 200
    assert response.json() == {
        "primary_color": "#c22126",
        "logo_url": "https://cdn.example/logo.png",
        "icon_url": None,
    }


@pytest.mark.parametrize("merchant_id", [None, "someone-else.myshopify.com"])
def test_a_merchant_must_name_its_own_merchant(probe, merchant_id) -> None:
    body = {"merchant_id": merchant_id} if merchant_id else {}
    assert _post(MERCHANT, **body).status_code == 403
    probe.assert_not_awaited()


@pytest.mark.parametrize(
    "error, code",
    [(UnsafeUrlError("private"), 400), (EgressNotGuardedError("proxied"), 503)],
)
def test_fetch_errors_map_to_status_codes(probe, error, code) -> None:
    probe.side_effect = error
    assert _post(ADMIN).status_code == code


def test_the_limit_is_ten_a_day_per_user(probe, monkeypatch) -> None:
    limiter = AsyncMock(
        return_value=RateLimitDecision(
            allowed=True, count=1, limit=10, retry_after_seconds=0
        )
    )
    monkeypatch.setattr(preview, "check_rate_limit", limiter)
    _post(MERCHANT, merchant_id="9b1086-18.myshopify.com")
    call = limiter.call_args
    assert (call.kwargs["identifier"], call.kwargs["limit"]) == ("m-1", 10)
    assert call.kwargs["window_seconds"] == 86400
    assert call.kwargs["fail_closed"]
