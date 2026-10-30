"""A merchant may connect only its own Shopify shop (N-CRM-1).

Nautilus registers each shop with its domain as the merchant id, and acts on
whatever shop an installation names — so the shop must be the merchant.
"""

import pytest

from app.crm.connectivity import onboarding as onboarding_module
from app.crm.connectivity.connectors import CONNECTORS
from app.crm.connectivity.onboarding import OnboardingError, onboard
from app.crm.connectivity.providers.base import ConnectorHandshakeError
from app.crm.connectivity.providers.shopify.onboard import (
    OnboardShopifyRequest,
    ShopifyOnboarder,
)
from tests.crm.doubles import FakeInstallationAccessor
from tests.crm.test_connectors_onboarding import (
    _installation_read,
    _patch_onboarding,
)

OWN = "own-store.myshopify.com"
OTHER = "other-store.myshopify.com"


async def test_own_shop_is_recorded() -> None:
    result = await ShopifyOnboarder().gather(
        OnboardShopifyRequest(merchant_id=OWN, shop_domain=OWN)
    )
    assert result.external_account_id == OWN


async def test_another_shop_is_refused() -> None:
    with pytest.raises(ConnectorHandshakeError, match="only its own shop"):
        await ShopifyOnboarder().gather(
            OnboardShopifyRequest(merchant_id=OWN, shop_domain=OTHER)
        )


def _real_shopify(monkeypatch: pytest.MonkeyPatch) -> FakeInstallationAccessor:
    installations = FakeInstallationAccessor(upsert_returns=_installation_read())
    _patch_onboarding(monkeypatch, installations=installations)
    monkeypatch.setattr(
        onboarding_module, "connector_for", lambda key: CONNECTORS["shopify"]
    )
    return installations


async def test_onboarding_another_shop_is_a_400_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installations = _real_shopify(monkeypatch)

    with pytest.raises(OnboardingError, match="only its own shop"):
        await onboard(OWN, "shopify", {"merchant_id": OWN, "shop_domain": OTHER})
    assert installations.written is None


async def test_the_body_merchant_id_cannot_override_the_authorised_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """?merchant_id=OWN authorises; a body naming OTHER as both merchant and
    shop must not let OWN connect OTHER's shop."""
    installations = _real_shopify(monkeypatch)

    with pytest.raises(OnboardingError, match="only its own shop"):
        await onboard(OWN, "shopify", {"merchant_id": OTHER, "shop_domain": OTHER})
    assert installations.written is None


async def test_own_shop_in_another_case_connects_and_is_stored_lowercase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installations = _real_shopify(monkeypatch)

    await onboard(
        OWN, "shopify", {"merchant_id": OWN, "shop_domain": " Own-Store.MyShopify.com "}
    )
    assert installations.written is not None
    assert installations.written["external_account_id"] == OWN


async def test_onboarding_own_shop_writes_the_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installations = _real_shopify(monkeypatch)

    await onboard(OWN, "shopify", {"merchant_id": OWN, "shop_domain": OWN})
    assert installations.written is not None
    assert installations.written["merchant_id"] == OWN
    assert installations.written["external_account_id"] == OWN
