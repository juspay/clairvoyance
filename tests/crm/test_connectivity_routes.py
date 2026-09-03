"""The two structural guarantees of the connectors surface (structure PR,
3 Sep 2026): every route declares the tenancy door, and one route class
translates this module's exceptions — so a route cannot forget the check
and two routes cannot map one family to two codes.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.crm.auth import MERCHANT_SCOPE_MARK
from app.crm.connectivity import api as connectivity_api
from app.crm.connectivity.onboarding import (
    OnboardingError,
    ResubscribeRefused,
    UnknownConnectorError,
)
from app.crm.connectivity.templates import (
    TemplateError,
    TemplateInUseError,
    TemplateNotFoundError,
)


def _declares_merchant_scope(route: APIRoute) -> bool:
    return any(
        getattr(dep.call, MERCHANT_SCOPE_MARK, False)
        for dep in route.dependant.dependencies
    )


def test_every_connectors_route_declares_the_tenancy_door() -> None:
    """The structural guarantee: walk the router, not the handlers."""
    routes = [r for r in connectivity_api.router.routes if isinstance(r, APIRoute)]
    assert len(routes) >= 11, "the router lost routes — the walk found too few"
    missing = [r.path for r in routes if not _declares_merchant_scope(r)]
    assert missing == [], f"routes without merchant_scope: {missing}"


def test_every_connectors_route_uses_the_translating_route_class() -> None:
    routes = [r for r in connectivity_api.router.routes if isinstance(r, APIRoute)]
    assert all(isinstance(r, connectivity_api.TranslatingRoute) for r in routes)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (UnknownConnectorError("telegram"), 404),
        (TemplateNotFoundError("no such template"), 404),
        (TemplateInUseError("still named by 2 runs"), 409),
        (ResubscribeRefused("the provider declined"), 409),
        (OnboardingError("bad code"), 400),
        (TemplateError("not from draft"), 400),
        (RuntimeError("not ours"), None),
    ],
)
def test_the_translator_maps_each_family_once(error: Exception, code: Any) -> None:
    translated = connectivity_api.translate(error)
    if code is None:
        assert translated is None
    else:
        assert translated is not None and translated.status_code == code


def _client(monkeypatch: pytest.MonkeyPatch, merchants=("shop",)) -> TestClient:
    app = FastAPI()
    app.include_router(connectivity_api.router, prefix="/connectors")
    app.dependency_overrides[get_current_user_with_rbac] = lambda: SimpleNamespace(
        role="user", username="u", merchant_ids=list(merchants), reseller_ids=[]
    )
    return TestClient(app)


def test_the_door_reads_the_merchant_from_a_post_body(monkeypatch) -> None:
    """merchant_scope reads the TenantScoped body on a POST and the handler
    still gets its parsed model — one read, two readers."""
    seen = {}

    async def fake_create(merchant_id, *args):
        seen["merchant"] = merchant_id
        raise TemplateError("stop here")

    monkeypatch.setattr(
        connectivity_api.contracts, "create_template_draft", fake_create
    )
    response = _client(monkeypatch).post(
        "/connectors/templates",
        json={
            "merchant_id": "shop",
            "channel": "whatsapp",
            "provider_account_ref": "waba-1",
            "name": "n",
            "language": "en",
            "components": [],
        },
    )
    assert response.status_code == 400 and seen["merchant"] == "shop"


def test_a_foreign_merchant_is_refused_before_the_handler_runs(monkeypatch) -> None:
    async def never(*args):
        raise AssertionError("the handler must not run for a foreign merchant")

    monkeypatch.setattr(connectivity_api.contracts, "list_templates", never)
    response = _client(monkeypatch).get(
        "/connectors/templates", params={"merchant_id": "someone-else"}
    )
    assert response.status_code == 403


def test_a_body_without_a_merchant_is_a_400_not_a_pass(monkeypatch) -> None:
    async def never(*args):
        raise AssertionError("no merchant, no handler")

    monkeypatch.setattr(connectivity_api.contracts, "onboard", never)
    response = _client(monkeypatch).post(
        "/connectors/whatsapp/onboard", json={"code": "x"}
    )
    assert response.status_code == 400
