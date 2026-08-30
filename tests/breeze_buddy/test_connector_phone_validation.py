from __future__ import annotations

import pytest

from app.api.routers.breeze_buddy.webhooks.breeze.abandonment import (
    build_abandoned_checkout_payload as build_breeze_payload,
)
from app.api.routers.breeze_buddy.webhooks.breeze.utils import (
    normalize_indian_phone as normalize_breeze_phone,
)
from app.api.routers.breeze_buddy.webhooks.woocommerce.order import (
    build_order_payload,
)
from app.api.routers.breeze_buddy.webhooks.woocommerce.utils import (
    normalize_indian_phone as normalize_woocommerce_phone,
)
from app.utils.phone_number import normalize_indian_phone_for_dialing


@pytest.mark.parametrize(
    "normalizer",
    [
        normalize_indian_phone_for_dialing,
        normalize_breeze_phone,
        normalize_woocommerce_phone,
    ],
)
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("98765 43210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("+44 20 7946 0958", "+442079460958"),
        ("", ""),
        ("not-a-phone", ""),
        ("12345", ""),
    ],
)
def test_connectors_normalize_or_skip_invalid_phone(
    normalizer, raw: str, expected: str
) -> None:
    assert normalizer(raw) == expected


def test_woocommerce_payload_contains_canonical_phone() -> None:
    phone, payload = build_order_payload(
        {"id": 1, "billing": {"phone": "98765-43210"}}, "Shop"
    )

    assert phone == "+919876543210"
    assert payload["customer_mobile_number"] == phone


def test_breeze_invalid_phone_preserves_skip_signal() -> None:
    phone, payload = build_breeze_payload(
        {"checkout_id": "checkout-1", "phone": "invalid"}, "Shop"
    )

    assert phone == ""
    assert payload["customer_mobile_number"] == ""
