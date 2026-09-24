"""Undialable phone numbers — validator and the push_lead_handler gate.

A number Plivo can't dial breaks the dispatcher: ``PlivoService.make_call``
swallows Plivo's rejection and returns None, and the no-SID branch reads
that as transient and re-defers 10s — against a counter that never
increments, so forever. One typo'd 24-digit number therefore became an
unbounded loop of P1 alerts that ops killed by hand.

Plivo itself dials up to 14 digits, with or without a country code — no
E.164 format check is needed, just a cap on digit count so a mistyped
number can't reach the dialer. ``push_lead_handler`` is the one gate that
enforces this, at the door, with a 404.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.routers.breeze_buddy.leads.handlers as lead_handlers
from app.ai.voice.agents.breeze_buddy.types.models import PushLeadRequest
from app.schemas.breeze_buddy.auth import UserInfo, UserRole
from app.utils.common import is_dialable

# The number from the production incident: 24 digits, well past the 14 cap.
BAD_NUMBER = "910226987555588908889023"
TEMPLATE_ID = "6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e"


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+919876543210", True),  # 12 digits, with country code
        ("9876543210", True),  # bare 10-digit — no country code needed
        ("+14155552671", True),  # 11 digits, non-Indian
        ("+91 98765 43210", True),  # punctuation doesn't count as a digit
        ("12345678901234", True),  # exactly 14 digits — the cap
        ("123456789012345", False),  # 15 digits — one over the cap
        (BAD_NUMBER, False),  # 24 digits
        ("", False),
        ("abc", False),  # no digits at all
        ("+", False),
    ],
)
def test_is_dialable(raw: str, expected: bool) -> None:
    assert is_dialable(raw) is expected


@pytest.mark.parametrize("raw", [None, 9876543210, [], {}, object()])
def test_non_string_is_not_dialable(raw: object) -> None:
    """The lead payload is Dict[str, Any] — a non-string must reject, not raise."""
    assert is_dialable(raw) is False


# ---------------------------------------------------------------------------
# The gate — push_lead_handler
# ---------------------------------------------------------------------------
#
# Campaigns need no gate of their own: create_campaign_handler builds a
# PushLeadRequest per row and calls this handler, collecting failures as
# CampaignLeadError(row, reason) — bad rows are skipped, the batch survives.

_MISSING = object()


def _req(phone: object) -> PushLeadRequest:
    payload: dict = {} if phone is _MISSING else {"customer_mobile_number": phone}
    return PushLeadRequest(
        request_id="order-1",
        payload=payload,
        template_id=TEMPLATE_ID,
        reseller_id="RESELLER",
    )


def _user() -> UserInfo:
    return UserInfo(id="u-test", username="tester", role=UserRole.ADMIN)


def _template() -> SimpleNamespace:
    return SimpleNamespace(
        id=TEMPLATE_ID,
        name="t",
        reseller_id="RESELLER",
        merchant_id=None,
        supported_channels=["voice"],
        expected_payload_schema=None,
    )


@pytest.fixture
def _template_resolves(monkeypatch):
    async def _get(_id):
        return _template()

    monkeypatch.setattr(lead_handlers, "get_template_by_id", _get)


@pytest.mark.parametrize(
    "phone",
    [
        BAD_NUMBER,
        "123456789012345",  # 15 digits — one over the cap
        "abc",
        "",
        12345,  # payload is Dict[str, Any] — a non-string must reject, not raise
    ],
)
@pytest.mark.asyncio
async def test_push_rejects_undialable_number(_template_resolves, phone):
    with pytest.raises(HTTPException) as exc:
        await lead_handlers.push_lead_handler(_req(phone), _user())
    assert exc.value.status_code == 404
    assert "too many digits" in exc.value.detail


@pytest.mark.parametrize(
    "phone",
    [
        "+919999999999",
        "9999999999",  # bare, no country code — still accepted
        "+14155552671",
        "12345678901234",  # exactly 14 digits — the cap, not over it
    ],
)
@pytest.mark.asyncio
async def test_push_lets_dialable_number_through(
    _template_resolves, monkeypatch, phone
):
    """A number within the digit cap sails past the gate — next stop, the
    blacklist check."""

    async def _boom(*_a, **_k):
        raise RuntimeError("reached blacklist check")

    monkeypatch.setattr(lead_handlers, "is_number_blacklisted", _boom)
    with pytest.raises(HTTPException) as exc:
        await lead_handlers.push_lead_handler(_req(phone), _user())
    # The handler's generic except wraps the sentinel in a 500 — the point is
    # it got PAST the gate (a rejection would be the 404 asserted above).
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_push_leaves_payload_unchanged(_template_resolves, monkeypatch):
    """No normalization happens — what the merchant sent is what gets
    blacklist-checked and dialed, untouched."""
    seen: list[str] = []

    async def _capture(phone, *_a, **_k):
        seen.append(phone)
        raise RuntimeError("reached blacklist check")

    monkeypatch.setattr(lead_handlers, "is_number_blacklisted", _capture)
    req = _req("9876543210")
    with pytest.raises(HTTPException):
        await lead_handlers.push_lead_handler(req, _user())

    assert seen == ["9876543210"]
    assert req.payload["customer_mobile_number"] == "9876543210"


@pytest.mark.asyncio
async def test_push_ignores_absent_phone(_template_resolves):
    """No customer_mobile_number at all is the template payload schema's
    business, not this gate's — the push proceeds (and here dies later on the
    missing call-execution config, a 404, rather than a phone-length 404)."""
    with pytest.raises(HTTPException) as exc:
        await lead_handlers.push_lead_handler(_req(_MISSING), _user())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_push_gate_runs_before_the_blacklist_lookup(
    _template_resolves, monkeypatch
):
    """Ordering is load-bearing: an overlong value must never reach the
    blacklist cache/DB hit."""
    calls: list[str] = []

    async def _track(phone, *_a, **_k):
        calls.append(phone)
        return False

    monkeypatch.setattr(lead_handlers, "is_number_blacklisted", _track)
    with pytest.raises(HTTPException):
        await lead_handlers.push_lead_handler(_req(BAD_NUMBER), _user())
    assert calls == []
