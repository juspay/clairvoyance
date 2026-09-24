"""``POST /assist/preview``: who may ask, how often, and what comes back.
Nothing is fetched for real: the probe and the brand lane are replaced."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ai.voice.agents.breeze_buddy.assist.engine.models import (
    BrandColor,
    BrandLook,
    SiteProfile,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    EgressNotGuardedError,
    UnsafeUrlError,
)
from app.ai.voice.agents.breeze_buddy.assist.onboarding import service
from app.api.routers.breeze_buddy.assist import preview
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
LOOK = BrandLook(
    colors=[BrandColor(role="primary", hex="#c22126", source="page")],
    logo_url="https://cdn.example/logo.png",
    sources=["page"],
    warnings=["brand provider not configured — skipped"],
)


def _limit(allowed: bool) -> AsyncMock:
    return AsyncMock(
        return_value=RateLimitDecision(
            allowed=allowed, count=61, limit=60, retry_after_seconds=30
        )
    )


@pytest.fixture()
def wired(monkeypatch):
    probe = AsyncMock(return_value=PROFILE)
    resolve = AsyncMock(return_value=LOOK)
    monkeypatch.setattr(service, "probe_site", probe)
    monkeypatch.setattr(service.brand_lane, "resolve", resolve)
    monkeypatch.setattr(preview, "check_rate_limit", _limit(True))
    return probe, resolve


def _post(user: UserInfo, **body):
    app = FastAPI()
    app.include_router(preview.router)
    app.dependency_overrides[get_current_user_with_rbac] = lambda: user
    payload = {"url": "https://store.example/", "reseller_id": "BB_SHOPIFY"}
    payload.update(body)
    return TestClient(app).post("/assist/preview", json=payload)


def test_returns_the_look_and_what_would_be_saved(wired):
    response = _post(ADMIN)

    assert response.status_code == 200
    data = response.json()
    assert data["colors"][0]["hex"] == "#c22126"
    assert data["appearance"] == {
        "primary_color": "#c22126",
        "header_logo_url": "https://cdn.example/logo.png",
    }
    assert data["warnings"] == ["brand provider not configured — skipped"]


def test_the_read_has_a_deadline_inside_the_consoles_wait(wired):
    _, resolve = wired
    assert _post(ADMIN).status_code == 200
    assert resolve.await_args is not None
    deadline = resolve.await_args.kwargs["deadline"]
    assert deadline is not None
    assert deadline - time.monotonic() <= 75


def test_a_merchant_can_preview_its_own_store(wired):
    response = _post(MERCHANT, merchant_id="9b1086-18.myshopify.com")
    assert response.status_code == 200


def test_a_merchant_needs_its_own_merchant_id_and_nothing_is_fetched(wired):
    probe, _ = wired
    assert _post(MERCHANT).status_code == 403
    assert _post(MERCHANT, merchant_id="someone-else.myshopify.com").status_code == 403
    assert (
        _post(
            MERCHANT, reseller_id="OTHER", merchant_id="9b1086-18.myshopify.com"
        ).status_code
        == 403
    )
    probe.assert_not_awaited()


def test_other_roles_are_refused(wired):
    probe, _ = wired
    viewer = ADMIN.model_copy(update={"role": UserRole.USER})
    assert _post(viewer).status_code == 403
    probe.assert_not_awaited()


def test_over_the_cap_is_429_before_any_fetch(wired, monkeypatch):
    probe, _ = wired
    monkeypatch.setattr(preview, "check_rate_limit", _limit(False))
    response = _post(ADMIN)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"
    probe.assert_not_awaited()


@pytest.mark.parametrize(
    "error,code",
    [(UnsafeUrlError("private"), 400), (EgressNotGuardedError("proxied"), 503)],
)
def test_fetch_errors_map_to_status_codes(wired, error, code):
    probe, _ = wired
    probe.side_effect = error
    assert _post(ADMIN).status_code == code
