"""onboarding.py: the health-detail branch on webhook subscription and the
retired-binding refusal on re-onboard — the rules canon states explicitly.
Accessor/provider/credential calls are monkeypatched; atomically() is
bypassed with a stub since these tests never touch a real connection."""

from types import SimpleNamespace
from typing import cast

import pytest

from app.crm.connectivity import onboarding
from app.crm.connectivity.db import DbTxn
from app.crm.connectivity.meta_graph import WhatsappProviderError
from app.crm.connectivity.onboarding import OnboardingError

_FAKE_TXN = cast(DbTxn, None)


async def _fake_atomically(fn, *args, **kwargs):
    return await fn(None, *args, **kwargs)


def _installation(**overrides) -> SimpleNamespace:
    base = dict(id="inst-1", merchant_id="m1", status="healthy")
    base.update(overrides)
    return SimpleNamespace(**base)


def _patch_onboard_prereqs(monkeypatch) -> None:
    """Everything onboard_whatsapp() needs before it reaches the webhook
    subscribe step — merchant lookup, the Meta token exchange, and
    credential persistence — stubbed to succeed so tests can focus on the
    branch under test."""

    async def fake_get_merchants_by_ids(ids):
        return [SimpleNamespace(reseller_id="r1")], None

    async def fake_exchange_code_for_token(code):
        return "short-lived"

    async def fake_exchange_for_long_lived_token(token):
        return "long-lived"

    async def fake_verify_phone_number(waba_id, phone_number_id, token):
        return None

    async def fake_find_credential(reseller_id, name):
        return None

    async def fake_create_credential(reseller_id, name, ctype, value, description):
        return SimpleNamespace(id="cred-1")

    monkeypatch.setattr(onboarding, "get_merchants_by_ids", fake_get_merchants_by_ids)
    monkeypatch.setattr(
        onboarding.whatsapp, "exchange_code_for_token", fake_exchange_code_for_token
    )
    monkeypatch.setattr(
        onboarding.whatsapp,
        "exchange_for_long_lived_token",
        fake_exchange_for_long_lived_token,
    )
    monkeypatch.setattr(
        onboarding.whatsapp, "verify_phone_number", fake_verify_phone_number
    )
    monkeypatch.setattr(onboarding, "_find_credential", fake_find_credential)
    monkeypatch.setattr(onboarding, "create_credential", fake_create_credential)
    monkeypatch.setattr(onboarding, "atomically", _fake_atomically)
    monkeypatch.setattr(
        onboarding.accessor, "upsert_installation", _fake_upsert_installation
    )
    monkeypatch.setattr(
        onboarding.accessor,
        "get_channel_binding_by_address",
        _fake_no_existing_binding,
    )
    monkeypatch.setattr(
        onboarding.accessor, "has_primary_binding", _fake_has_no_primary_binding
    )
    monkeypatch.setattr(
        onboarding.accessor, "upsert_channel_binding", _fake_upsert_channel_binding
    )


captured_health_detail = {}


async def _fake_upsert_installation(
    txn,
    merchant_id,
    connector_key,
    waba_id,
    display_label,
    credential_id,
    status,
    health_detail,
):
    captured_health_detail["value"] = health_detail
    return _installation(status=status)


async def _fake_no_existing_binding(txn, merchant_id, channel, address):
    return None


async def _fake_has_no_primary_binding(txn, merchant_id, channel):
    return False


async def _fake_upsert_channel_binding(
    txn, merchant_id, channel, installation_id, address, is_primary
):
    return SimpleNamespace(id="bind-1", is_primary=is_primary, status="active")


async def test_subscribe_success_sets_subscribed_health(monkeypatch) -> None:
    _patch_onboard_prereqs(monkeypatch)
    captured_health_detail.clear()

    async def fake_subscribe_to_webhooks(waba_id, token):
        return None

    monkeypatch.setattr(
        onboarding.whatsapp, "subscribe_to_webhooks", fake_subscribe_to_webhooks
    )

    await onboarding.onboard_whatsapp("m1", "code", "waba-1", "+15550001111")

    assert captured_health_detail["value"]["level"] == "subscribed"
    assert captured_health_detail["value"]["why"] == onboarding._NO_RECEIVER_WHY


async def test_subscribe_failure_sets_authenticated_health(monkeypatch) -> None:
    _patch_onboard_prereqs(monkeypatch)
    captured_health_detail.clear()

    async def fake_subscribe_to_webhooks(waba_id, token):
        raise WhatsappProviderError("Meta returned 400")

    monkeypatch.setattr(
        onboarding.whatsapp, "subscribe_to_webhooks", fake_subscribe_to_webhooks
    )

    await onboarding.onboard_whatsapp("m1", "code", "waba-1", "+15550001111")

    assert captured_health_detail["value"]["level"] == "authenticated"
    assert "webhook subscription failed" in captured_health_detail["value"]["why"]


async def test_reonboard_refuses_a_retired_binding(monkeypatch) -> None:
    async def fake_upsert_installation(*args, **kwargs):
        return _installation()

    async def fake_get_retired_binding(txn, merchant_id, channel, address):
        return SimpleNamespace(status="retired")

    monkeypatch.setattr(
        onboarding.accessor, "upsert_installation", fake_upsert_installation
    )
    monkeypatch.setattr(
        onboarding.accessor,
        "get_channel_binding_by_address",
        fake_get_retired_binding,
    )

    with pytest.raises(OnboardingError, match="retired"):
        await onboarding._onboard_in_txn(
            _FAKE_TXN, "m1", "waba-1", "+15550001111", "cred-1", None, {}
        )


async def test_disconnect_pauses_bindings_after_revoking_installation(
    monkeypatch,
) -> None:
    """ATOMIC law: a revoked installation must never leave a binding that
    still claims to be an active send route — pause must fire, and only
    after the installation itself is revoked."""
    calls = []

    async def fake_disconnect_installation(txn, merchant_id, installation_id):
        calls.append(("disconnect_installation", merchant_id, installation_id))
        return _installation(status="revoked")

    async def fake_pause_bindings_for_installation(txn, merchant_id, installation_id):
        calls.append(("pause_bindings_for_installation", merchant_id, installation_id))

    monkeypatch.setattr(
        onboarding.accessor, "disconnect_installation", fake_disconnect_installation
    )
    monkeypatch.setattr(
        onboarding.accessor,
        "pause_bindings_for_installation",
        fake_pause_bindings_for_installation,
    )

    result = await onboarding._disconnect_in_txn(_FAKE_TXN, "m1", "inst-1")

    assert result is not None
    assert calls == [
        ("disconnect_installation", "m1", "inst-1"),
        ("pause_bindings_for_installation", "m1", "inst-1"),
    ]


async def test_disconnect_of_foreign_installation_never_pauses_bindings(
    monkeypatch,
) -> None:
    """Fail closed (CRM law #6): if installation_id doesn't belong to
    merchant_id, disconnect_installation returns None and nothing about that
    installation's bindings gets touched."""
    pause_calls = []

    async def fake_disconnect_installation(txn, merchant_id, installation_id):
        return None

    async def fake_pause_bindings_for_installation(txn, merchant_id, installation_id):
        pause_calls.append((merchant_id, installation_id))

    monkeypatch.setattr(
        onboarding.accessor, "disconnect_installation", fake_disconnect_installation
    )
    monkeypatch.setattr(
        onboarding.accessor,
        "pause_bindings_for_installation",
        fake_pause_bindings_for_installation,
    )

    result = await onboarding._disconnect_in_txn(_FAKE_TXN, "m1", "not-mine")

    assert result is None
    assert pause_calls == []


async def test_reonboard_reactivates_a_paused_binding_as_primary(monkeypatch) -> None:
    """No existing primary binding for this channel -> the reactivated
    binding becomes primary."""

    async def fake_upsert_installation(*args, **kwargs):
        return _installation()

    async def fake_get_paused_binding(txn, merchant_id, channel, address):
        return SimpleNamespace(status="paused")

    async def fake_has_no_primary_binding(txn, merchant_id, channel):
        return False

    upsert_calls = []
    returned_bindings = []

    async def fake_upsert_channel_binding(
        txn, merchant_id, channel, installation_id, address, is_primary
    ):
        upsert_calls.append(is_primary)
        binding = SimpleNamespace(id="bind-1", is_primary=is_primary, status="active")
        returned_bindings.append(binding)
        return binding

    monkeypatch.setattr(
        onboarding.accessor, "upsert_installation", fake_upsert_installation
    )
    monkeypatch.setattr(
        onboarding.accessor,
        "get_channel_binding_by_address",
        fake_get_paused_binding,
    )
    monkeypatch.setattr(
        onboarding.accessor, "has_primary_binding", fake_has_no_primary_binding
    )
    monkeypatch.setattr(
        onboarding.accessor, "upsert_channel_binding", fake_upsert_channel_binding
    )

    result = await onboarding._onboard_in_txn(
        _FAKE_TXN, "m1", "waba-1", "+15550001111", "cred-1", None, {}
    )

    assert result.id == "inst-1"
    assert upsert_calls == [True]
    assert returned_bindings[0].status == "active"
