"""Undialable phone numbers — validator, push gate, dispatch gate.

A number that is not valid E.164 can never complete a call. Plivo rejects
it server-side, ``PlivoService.make_call`` swallows the exception and
returns None, and the dispatcher's no-SID branch reads that as transient
and re-defers 10s — against a counter that never increments, so forever.
One typo'd 24-digit number therefore became an unbounded loop of P1 alerts
that ops killed by hand.

Two gates close it, and both are exercised here alongside the validator
itself:

  1. ``push_lead_handler`` rejects it at the door with a 400.
  2. the dispatch worker finalizes anything already in BACKLOG as
     FINISHED / INVALID_PHONE before spending a telephony number, a
     channel token or greeting TTS on it.

The dispatch fixtures (``harness``, ``fake_redis``) live in
``tests/breeze_buddy/dispatch/conftest.py``; they are imported below so
this stays one file rather than three.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.routers.breeze_buddy.leads.handlers as lead_handlers
from app.ai.voice.agents.breeze_buddy.dispatch import worker as w
from app.ai.voice.agents.breeze_buddy.dispatch.channel_semaphore import (
    channel_tokens_available,
    init_channel_semaphore,
)
from app.ai.voice.agents.breeze_buddy.dispatch.keys import READY_LIST
from app.ai.voice.agents.breeze_buddy.types.models import PushLeadRequest
from app.schemas import LeadCallStatus
from app.schemas.breeze_buddy.auth import UserInfo, UserRole
from app.utils.common import is_dialable, normalize_e164

# Imported for their side effect of registering here as fixtures — pytest
# only auto-injects a conftest's fixtures into tests beneath it.
from tests.breeze_buddy.dispatch.conftest import (  # noqa: F401
    fake_redis,
    harness,
    make_lead,
)

# The number from the production incident: 24 digits, where E.164 caps at 15.
BAD_NUMBER = "910226987555588908889023"
TEMPLATE_ID = "6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e"


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------
#
# The phone helpers in ``app/utils/common`` deliberately duplicate the crm
# normalizer (boundary rule 4 forbids buddy code importing app.crm.shared),
# so they are pinned to the same table as ``tests/crm/test_normalize.py``.
# If these two tests disagree, the duplicate has drifted.


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+919876543210", "+919876543210"),
        ("9876543210", "+919876543210"),  # bare 10-digit -> +91
        ("09876543210", "+919876543210"),  # leading 0
        ("919876543210", "+919876543210"),  # 91-prefixed, no +
        ("00919876543210", "+919876543210"),  # international 00
        ("+91 98765 43210", "+919876543210"),  # spaces stripped
        ("+91-98765-43210", "+919876543210"),  # punctuation stripped
        ("+14155552671", "+14155552671"),  # non-Indian E.164 untouched
        ("", None),
        ("garbage", None),
        ("+0123", None),  # E.164 cannot start +0
        ("123", None),  # too short to qualify
    ],
)
def test_normalize_e164(raw: str, expected: str | None) -> None:
    assert normalize_e164(raw) == expected


def test_reported_overlong_number_is_rejected() -> None:
    assert normalize_e164(BAD_NUMBER) is None
    assert is_dialable(BAD_NUMBER) is False


@pytest.mark.parametrize("raw", [None, 919876543210, [], {}, object()])
def test_non_string_is_not_dialable(raw: object) -> None:
    """The lead payload is Dict[str, Any] — a non-string must reject, not raise."""
    assert normalize_e164(raw) is None
    assert is_dialable(raw) is False


def test_is_dialable_agrees_with_normalize() -> None:
    for raw in ["+919876543210", "9876543210", "garbage", "", "+91"]:
        assert is_dialable(raw) is (normalize_e164(raw) is not None)


# ---------------------------------------------------------------------------
# Gate 1 — push_lead_handler
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
        "abc",
        "",
        "+91",
        "123",
        "+0123456789",
        12345,  # payload is Dict[str, Any] — a non-string must reject, not raise
    ],
)
@pytest.mark.asyncio
async def test_push_rejects_undialable_number(_template_resolves, phone):
    with pytest.raises(HTTPException) as exc:
        await lead_handlers.push_lead_handler(_req(phone), _user())
    assert exc.value.status_code == 400
    assert "dialable" in exc.value.detail


@pytest.mark.parametrize(
    "phone",
    [
        "+919999999999",
        "9999999999",
        "09999999999",
        "919999999999",
        "+91 99999 99999",
        "+14155552671",
    ],
)
@pytest.mark.asyncio
async def test_push_lets_dialable_number_through(
    _template_resolves, monkeypatch, phone
):
    """A valid number sails past the gate — next stop, the blacklist check."""

    async def _boom(*_a, **_k):
        raise RuntimeError("reached blacklist check")

    monkeypatch.setattr(lead_handlers, "is_number_blacklisted", _boom)
    with pytest.raises(HTTPException) as exc:
        await lead_handlers.push_lead_handler(_req(phone), _user())
    # The handler's generic except wraps the sentinel in a 500 — the point is
    # it got PAST the gate (a rejection would be the 400 asserted above).
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_push_normalizes_payload_and_blacklist_lookup(
    _template_resolves, monkeypatch
):
    """The gate doesn't just validate — it rewrites the payload to the
    normalized form, so what's stored, blacklist-checked and eventually
    dialed all agree on one canonical shape."""
    seen: list[str] = []

    async def _capture(phone, *_a, **_k):
        seen.append(phone)
        raise RuntimeError("reached blacklist check")

    monkeypatch.setattr(lead_handlers, "is_number_blacklisted", _capture)
    req = _req("9876543210")
    with pytest.raises(HTTPException):
        await lead_handlers.push_lead_handler(req, _user())

    assert seen == ["+919876543210"]
    assert req.payload["customer_mobile_number"] == "+919876543210"


@pytest.mark.asyncio
async def test_push_ignores_absent_phone(_template_resolves):
    """No customer_mobile_number at all is the template payload schema's
    business, not this gate's — the push proceeds (and here dies later on the
    missing call-execution config, a 404, rather than a phone-format 400)."""
    with pytest.raises(HTTPException) as exc:
        await lead_handlers.push_lead_handler(_req(_MISSING), _user())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_push_gate_runs_before_the_blacklist_lookup(
    _template_resolves, monkeypatch
):
    """Ordering is load-bearing: a garbage value must never reach the
    blacklist cache/DB hit."""
    calls: list[str] = []

    async def _track(phone, *_a, **_k):
        calls.append(phone)
        return False

    monkeypatch.setattr(lead_handlers, "is_number_blacklisted", _track)
    with pytest.raises(HTTPException):
        await lead_handlers.push_lead_handler(_req(BAD_NUMBER), _user())
    assert calls == []


# ---------------------------------------------------------------------------
# Gate 2 — the dispatch worker
# ---------------------------------------------------------------------------


async def test_dispatch_finalizes_undialable_lead_without_spending_resources(
    harness, fake_redis
):
    """Catches leads already sitting in BACKLOG, and any path that creates a
    lead without going through push_lead_handler.

    The assertions on the channel token and the number pick are the point:
    this happens BEFORE any telephony resource or greeting TTS is spent.
    """
    lead = make_lead("lead-bad-phone", phone=BAD_NUMBER)
    harness.add_lead(lead)

    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-badphone")
    await worker._iteration(session=None)

    # Same outcome the presence/type gate writes — this one caught the format.
    assert lead.status == LeadCallStatus.FINISHED
    assert lead.outcome == "INVALID_PHONE"

    # No dial, and no resource ever taken: token untouched, number never
    # acquired/released, no rate-limit peek, and — the actual bug — no retry
    # scheduled, so this cannot loop.
    assert harness.call_recorder.calls == []
    assert await channel_tokens_available(harness.number.id) == 1
    assert harness.released_numbers == []
    assert harness.rate_limit_peeks == []
    assert harness.deferred == []

    # Lock released, so the row is not left stuck.
    assert lead.id in harness.released_locks


async def test_dispatch_leaves_dialable_lead_alone(harness, fake_redis):
    """The gate must not catch ordinary numbers — a valid one still dials."""
    lead = make_lead("lead-ok-phone", phone="+919876543210")
    harness.add_lead(lead)

    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-okphone")
    await worker._iteration(session=None)

    assert lead.outcome != "INVALID_PHONE"
    assert len(harness.call_recorder.calls) == 1


async def test_dispatch_dials_normalized_number(harness, fake_redis):
    """A bare, country-code-less number is dialable (gate 1 lets it through
    at push time already) — the worker must dial its normalized E.164 form,
    not the raw payload string, or the provider gets a number it can't
    route."""
    lead = make_lead("lead-bare-phone", phone="9876543210")
    harness.add_lead(lead)

    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)

    worker = w.Worker(worker_uuid="w-barephone")
    await worker._iteration(session=None)

    assert lead.outcome != "INVALID_PHONE"
    assert harness.call_recorder.calls[0]["to"] == "+919876543210"
