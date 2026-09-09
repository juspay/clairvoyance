"""Public ``GET /widget/storefront-config`` — merchant-domain resolve.

Route-level via TestClient on a bare app (pattern:
tests/test_tts_catalog_endpoint.py). The DB accessor and the Redis rate
limiter are monkeypatched on the handler module; the origin allowlist and
domain normalization run for real — they ARE the subject.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ai.voice.agents.breeze_buddy.assist.platforms.shopify.tenancy import (
    assist_tenant,
)
from app.api.routers.breeze_buddy.widget import router, storefront
from app.schemas.breeze_buddy.widget_config import WidgetConfigResponse
from app.services.redis.rate_limit import RateLimitDecision

MERCHANT_DOMAIN = "yuqe0m-xr.myshopify.com"
STANDALONE = assist_tenant("buddy-assist", MERCHANT_DOMAIN)
SHARED = assist_tenant("breeze-buddy", MERCHANT_DOMAIN)
STOREFRONT_ORIGIN = "https://zodiaconline.com"
UPDATED_AT = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)


def _cfg(**overrides) -> WidgetConfigResponse:
    fields = {
        "id": "00000000-0000-0000-0000-000000000020",
        "reseller_id": STANDALONE[0],
        "merchant_id": STANDALONE[1],
        "public_widget_key": "public-key-123",
        "template_id": "00000000-0000-0000-0000-000000000002",
        "allowed_origins": [f"https://{MERCHANT_DOMAIN}", STOREFRONT_ORIGIN],
        "active": True,
        "appearance": {"header_title": "Zodiac Assist", "primary_color": "#111"},
        "updated_at": UPDATED_AT,
    }
    fields.update(overrides)
    return WidgetConfigResponse(**fields)


def _allow(limit: int = 600) -> RateLimitDecision:
    return RateLimitDecision(allowed=True, count=1, limit=limit, retry_after_seconds=0)


def _deny(limit: int = 600) -> RateLimitDecision:
    return RateLimitDecision(
        allowed=False, count=limit, limit=limit, retry_after_seconds=42
    )


@pytest.fixture()
def lookup(monkeypatch) -> AsyncMock:
    mock = AsyncMock(return_value=_cfg())
    monkeypatch.setattr(storefront, "get_widget_config_by_reseller_merchant", mock)
    monkeypatch.setattr(storefront, "enforce_widget_ip_limit", AsyncMock())
    monkeypatch.setattr(
        storefront, "check_rate_limit", AsyncMock(return_value=_allow())
    )
    return mock


@pytest.fixture()
def client(lookup) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_resolves_shop_to_config(client: TestClient, lookup: AsyncMock) -> None:
    response = client.get(
        "/widget/storefront-config",
        params={"merchant_domain": MERCHANT_DOMAIN},
        headers={"Origin": STOREFRONT_ORIGIN},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["tenant"] == "public-key-123"
    assert body["merchant_domain"] == MERCHANT_DOMAIN
    assert body["appearance"] == {
        "header_title": "Zodiac Assist",
        "primary_color": "#111",
    }
    assert body["settings_revision"] == UPDATED_AT.isoformat()
    assert body["cache_ttl_seconds"] == 60
    assert response.headers["ETag"].startswith('"') and response.headers[
        "ETag"
    ].endswith('"')
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["Access-Control-Expose-Headers"] == "ETag"
    lookup.assert_awaited_once_with(*STANDALONE)


def test_normalizes_shop_before_lookup(client: TestClient, lookup: AsyncMock) -> None:
    response = client.get(
        "/widget/storefront-config",
        params={"merchant_domain": f"HTTPS://{MERCHANT_DOMAIN.upper()}/"},
        headers={"Origin": STOREFRONT_ORIGIN},
    )
    assert response.status_code == 200
    lookup.assert_awaited_once_with(*STANDALONE)


def test_unknown_and_inactive_are_the_same_404(
    client: TestClient, lookup: AsyncMock
) -> None:
    lookup.return_value = None
    unknown = client.get(
        "/widget/storefront-config",
        params={"merchant_domain": MERCHANT_DOMAIN},
        headers={"Origin": STOREFRONT_ORIGIN},
    )
    lookup.return_value = _cfg(active=False)
    inactive = client.get(
        "/widget/storefront-config",
        params={"merchant_domain": MERCHANT_DOMAIN},
        headers={"Origin": STOREFRONT_ORIGIN},
    )
    assert unknown.status_code == inactive.status_code == 404
    assert unknown.json() == inactive.json()


def test_origin_missing_or_foreign_is_403(client: TestClient) -> None:
    missing = client.get(
        "/widget/storefront-config", params={"merchant_domain": MERCHANT_DOMAIN}
    )
    foreign = client.get(
        "/widget/storefront-config",
        params={"merchant_domain": MERCHANT_DOMAIN},
        headers={"Origin": "https://evil.example"},
    )
    assert missing.status_code == 403
    assert foreign.status_code == 403


def test_falls_back_to_shared_voice_tenant(
    client: TestClient, lookup: AsyncMock
) -> None:
    # No standalone (BB_ASSIST) tenant → the breeze-buddy tab's BB_SHOPIFY
    # tenant serves the storefront. This is every pre-existing live tenant.
    shared_cfg = _cfg(reseller_id=SHARED[0], merchant_id=SHARED[1])

    async def by_namespace(reseller_id, merchant_id):
        return shared_cfg if (reseller_id, merchant_id) == SHARED else None

    lookup.side_effect = by_namespace
    response = client.get(
        "/widget/storefront-config",
        params={"merchant_domain": MERCHANT_DOMAIN},
        headers={"Origin": STOREFRONT_ORIGIN},
    )
    assert response.status_code == 200
    assert response.json()["tenant"] == "public-key-123"


def test_inactive_standalone_never_shadows_live_shared_tenant(
    client: TestClient, lookup: AsyncMock
) -> None:
    inactive_standalone = _cfg(active=False)
    live_shared = _cfg(reseller_id=SHARED[0], merchant_id=SHARED[1])

    async def by_namespace(reseller_id, merchant_id):
        return (
            inactive_standalone
            if (reseller_id, merchant_id) == STANDALONE
            else live_shared
        )

    lookup.side_effect = by_namespace
    response = client.get(
        "/widget/storefront-config",
        params={"merchant_domain": MERCHANT_DOMAIN},
        headers={"Origin": STOREFRONT_ORIGIN},
    )
    assert response.status_code == 200


def test_referer_fallback_matches_allowlist(client: TestClient) -> None:
    response = client.get(
        "/widget/storefront-config",
        params={"merchant_domain": MERCHANT_DOMAIN},
        headers={"Referer": f"{STOREFRONT_ORIGIN}/products/blue-shirt"},
    )
    assert response.status_code == 200


def test_malformed_shop_is_400_and_never_hits_db(
    client: TestClient, lookup: AsyncMock
) -> None:
    response = client.get(
        "/widget/storefront-config",
        params={"merchant_domain": "javascript:alert(1)"},
        headers={"Origin": STOREFRONT_ORIGIN},
    )
    assert response.status_code == 400
    lookup.assert_not_awaited()


def test_probe_limit_runs_before_any_db_lookup(
    client: TestClient, lookup: AsyncMock, monkeypatch
) -> None:
    # An anonymous caller spraying unknown-but-valid domains must be
    # bounded BEFORE the database is consulted — the merchant-scoped
    # limiter can never cover this case (it needs a resolved config).
    monkeypatch.setattr(storefront, "check_rate_limit", AsyncMock(return_value=_deny()))
    response = client.get(
        "/widget/storefront-config",
        params={"merchant_domain": "sprayed-domain.example.com"},
        headers={"Origin": STOREFRONT_ORIGIN},
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "42"
    lookup.assert_not_awaited()


def test_preflight_is_open(client: TestClient) -> None:
    response = client.options("/widget/storefront-config")
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"
    assert "if-none-match" in response.headers["access-control-allow-headers"]


def _get(client: TestClient, **headers: str):
    return client.get(
        "/widget/storefront-config",
        params={"merchant_domain": MERCHANT_DOMAIN},
        headers={"Origin": STOREFRONT_ORIGIN, **headers},
    )


def test_matching_if_none_match_is_304_and_skips_the_per_widget_limit(
    client: TestClient, monkeypatch
) -> None:
    etag = _get(client).headers["ETag"]
    limiter = AsyncMock()
    monkeypatch.setattr(storefront, "enforce_widget_ip_limit", limiter)
    revalidated = _get(client, **{"If-None-Match": etag})
    assert revalidated.status_code == 304
    assert revalidated.content == b""
    assert revalidated.headers["ETag"] == etag
    assert revalidated.headers["Cache-Control"] == "no-cache"
    limiter.assert_not_awaited()


def test_stale_or_weak_if_none_match_is_a_full_200(client: TestClient) -> None:
    etag = _get(client).headers["ETag"]
    assert _get(client, **{"If-None-Match": '"nope"'}).status_code == 200
    assert _get(client, **{"If-None-Match": f"W/{etag}"}).status_code == 304
    assert _get(client, **{"If-None-Match": "*"}).status_code == 304


def test_a_valid_etag_from_a_foreign_origin_is_still_403(client: TestClient) -> None:
    # The ETag is not a credential. Revalidation is answered only after the
    # origin allowlist has run, so a tag lifted from a real storefront buys
    # a foreign page nothing.
    etag = _get(client).headers["ETag"]
    response = client.get(
        "/widget/storefront-config",
        params={"merchant_domain": MERCHANT_DOMAIN},
        headers={"Origin": "https://evil.example", "If-None-Match": etag},
    )
    assert response.status_code == 403


def test_the_probe_cap_still_bounds_revalidation(
    client: TestClient, monkeypatch
) -> None:
    # The per-IP probe cap is the only limiter a 304 passes through, so it
    # has to run before the revalidation shortcut — otherwise conditional
    # requests would be free and unbounded.
    etag = _get(client).headers["ETag"]
    monkeypatch.setattr(storefront, "check_rate_limit", AsyncMock(return_value=_deny()))
    assert _get(client, **{"If-None-Match": etag}).status_code == 429


def test_etag_changes_when_the_appearance_changes(
    client: TestClient, lookup: AsyncMock
) -> None:
    before = _get(client).headers["ETag"]
    lookup.return_value = _cfg(
        appearance={"header_title": "Zodiac Assist", "primary_color": "#222"}
    )
    after = _get(client).headers["ETag"]
    assert before != after
    assert _get(client, **{"If-None-Match": before}).status_code == 200
