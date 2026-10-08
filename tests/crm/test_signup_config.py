"""GET /connectors/{key}/signup: the public half of a connector's browser
signup, served from the backend env so every console build reads the same
values. WhatsApp answers with Meta's app id and Embedded Signup
configuration id; a deployment missing either says 'not configured' instead
of handing the console a popup that cannot work; a connector without a
browser signup (Shopify installs from its App Store) is a 404 like an
unknown one; and the secret never appears."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.crm.connectivity import api as connectivity_api, onboarding
from app.crm.connectivity.onboarding import UnknownConnectorError
from app.crm.connectivity.providers.whatsapp import onboard as whatsapp_onboard


def _env(monkeypatch, app_id: str, config_id: str) -> None:
    monkeypatch.setattr(whatsapp_onboard, "META_APP_ID", app_id)
    monkeypatch.setattr(whatsapp_onboard, "META_ES_CONFIG_ID", config_id)
    monkeypatch.setattr(whatsapp_onboard, "META_APP_SECRET", "s3cret")
    monkeypatch.setattr(whatsapp_onboard, "META_WHATSAPP_GRAPH_VERSION", "v23.0")


def test_whatsapp_serves_the_app_and_signup_configuration(monkeypatch) -> None:
    _env(monkeypatch, "1111222233334444", " 5555666677778888 ")
    config = onboarding.signup_config("whatsapp")
    assert config.configured is True
    assert config.app_id == "1111222233334444"
    assert config.config_id == "5555666677778888"
    assert config.graph_version == "v23.0"
    # The secret half of the handshake never leaves the backend.
    assert "s3cret" not in config.model_dump_json()


@pytest.mark.parametrize(
    ("app_id", "config_id"), [("", "1335"), ("4079", ""), ("", "")]
)
def test_a_deployment_missing_either_id_is_not_configured(
    monkeypatch, app_id, config_id
) -> None:
    """Half a configuration is no configuration: the console must not open a
    popup Meta will refuse, and must not be handed the half it has."""
    _env(monkeypatch, app_id, config_id)
    config = onboarding.signup_config("whatsapp")
    assert config.configured is False
    assert config.app_id is None and config.config_id is None


@pytest.mark.parametrize("key", ["shopify", "carrier_pigeon"])
def test_no_browser_signup_is_a_404(key) -> None:
    with pytest.raises(UnknownConnectorError):
        onboarding.signup_config(key)


def _client(merchants=("shop",)) -> TestClient:
    app = FastAPI()
    app.include_router(connectivity_api.router, prefix="/connectors")
    app.dependency_overrides[get_current_user_with_rbac] = lambda: SimpleNamespace(
        role="merchant", username="u", merchant_ids=list(merchants), reseller_ids=[]
    )
    return TestClient(app)


def test_the_route_serves_it_to_the_merchants_own_users(monkeypatch) -> None:
    _env(monkeypatch, "4079", "1335")
    response = _client().get(
        "/connectors/whatsapp/signup", params={"merchant_id": "shop"}
    )
    assert response.status_code == 200
    assert response.json() == {
        "connector_key": "whatsapp",
        "configured": True,
        "app_id": "4079",
        "config_id": "1335",
        "graph_version": "v23.0",
    }


def test_the_route_is_scoped_like_onboarding(monkeypatch) -> None:
    _env(monkeypatch, "4079", "1335")
    client = _client(merchants=("other",))
    assert (
        client.get(
            "/connectors/whatsapp/signup", params={"merchant_id": "shop"}
        ).status_code
        == 403
    )
    # No merchant named at all: the scope cannot be checked, so no answer.
    assert client.get("/connectors/whatsapp/signup").status_code in (400, 403, 422)


def test_the_route_404s_a_connector_without_a_browser_signup() -> None:
    response = _client().get(
        "/connectors/shopify/signup", params={"merchant_id": "shop"}
    )
    assert response.status_code == 404
