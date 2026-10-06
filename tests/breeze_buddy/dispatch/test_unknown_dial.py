"""
A dial whose provider reply never arrived (Plivo read timeout).

Plivo may have placed that call, so the dialler must neither redial nor
release. It holds the lead PROCESSING with no call_id; Plivo's webhook claims
it through the lead_id + dial_at echoed on the answer/hangup URLs; and if no
webhook ever comes, the stuck sweep puts the same lead back to be dialled.
"""

from __future__ import annotations

import http.client
import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple, cast
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
import requests
import urllib3
from starlette.requests import Request

from app.ai.voice.agents.breeze_buddy.dispatch import worker as w
from app.ai.voice.agents.breeze_buddy.dispatch.channel_semaphore import (
    channel_tokens_available,
    init_channel_semaphore,
)
from app.ai.voice.agents.breeze_buddy.dispatch.keys import READY_LIST
from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    DIAL_OUTCOME_UNKNOWN,
    UNKNOWN_DIAL_META_KEY,
    dial_ref_time,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo.plivo import (
    PlivoProvider,
)
from app.api.routers.breeze_buddy.telephony.answer import handlers as answer_mod
from app.api.routers.breeze_buddy.telephony.callbacks import handlers as status_mod
from app.core.config import static
from app.schemas import CallDirection, ExecutionMode, LeadCallStatus
from app.schemas.breeze_buddy.core import LeadCallTracker
from tests.breeze_buddy.dispatch.conftest import make_lead

DIALLED_AT = datetime(2026, 10, 2, 4, 32, 4, 123456, tzinfo=timezone.utc)
DIAL_REF = {"lead_id": "lead-1", "dial_at": DIALLED_AT.isoformat()}


# ---------------------------------------------------------------------------
# Plivo adapter: a timeout is "unknown", not "not placed"
# ---------------------------------------------------------------------------


class _Calls:
    def __init__(self, outcome: Any):
        self.outcome = outcome
        self.kwargs: Dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def plivo(monkeypatch: pytest.MonkeyPatch) -> PlivoProvider:
    monkeypatch.setattr(static, "PLIVO_AUTH_ID", "MAXXXXXXXXXXXXXXXXXX")
    monkeypatch.setattr(static, "PLIVO_AUTH_TOKEN", "token")
    return PlivoProvider(aiohttp_session=None)


def _plivo_returns(provider: PlivoProvider, outcome: Any) -> _Calls:
    calls = _Calls(outcome)
    provider.client = cast(Any, SimpleNamespace(calls=calls))
    return calls


def test_a_read_timeout_is_an_unknown_outcome(plivo: PlivoProvider) -> None:
    """Sent, no reply: Plivo may have placed it — never reported as None."""
    _plivo_returns(plivo, requests.exceptions.ReadTimeout("read timeout=15"))

    assert plivo.make_call("+919999999999", "+918000000000", dial_ref=DIAL_REF) == {
        "status": DIAL_OUTCOME_UNKNOWN,
        "sid": None,
    }


def test_a_dial_that_never_reached_plivo_is_still_not_placed(
    plivo: PlivoProvider,
) -> None:
    _plivo_returns(plivo, requests.exceptions.ConnectTimeout("connect timeout"))

    assert plivo.make_call("+919999999999", "+918000000000", dial_ref=DIAL_REF) is None


def test_the_dial_ref_rides_on_both_webhook_urls(plivo: PlivoProvider) -> None:
    calls = _plivo_returns(plivo, SimpleNamespace(request_uuid="uuid-1"))

    result = plivo.make_call(
        "+919999999999",
        "+918000000000",
        reseller_id="res-1",
        template_name="tmpl-1",
        dial_ref=DIAL_REF,
    )

    assert result == {"status": "call_initiated", "sid": "uuid-1"}
    answer = parse_qs(urlparse(calls.kwargs["answer_url"]).query)
    hangup = parse_qs(urlparse(calls.kwargs["hangup_url"]).query)
    assert answer == {
        "reseller_id": ["res-1"],
        "template": ["tmpl-1"],
        "lead_id": ["lead-1"],
        "dial_at": [DIALLED_AT.isoformat()],
    }
    assert hangup == {"lead_id": ["lead-1"], "dial_at": [DIALLED_AT.isoformat()]}


def test_without_a_dial_ref_the_urls_are_unchanged(plivo: PlivoProvider) -> None:
    calls = _plivo_returns(plivo, SimpleNamespace(request_uuid="uuid-1"))

    plivo.make_call("+919999999999", "+918000000000", reseller_id="res-1")

    assert calls.kwargs["answer_url"].endswith("/plivo/answer?reseller_id=res-1")
    assert calls.kwargs["hangup_url"].endswith("/plivo/callback/status")


# ---------------------------------------------------------------------------
# Worker: hold the lead like a placed call — no redial, nothing released
# ---------------------------------------------------------------------------


def _reply_times_out(harness: Any, monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    dial_refs: List[Any] = []

    async def _no_reply(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        dial_refs.append(kwargs.get("dial_ref"))
        return {"status": DIAL_OUTCOME_UNKNOWN, "sid": None}

    monkeypatch.setattr(harness.call_recorder, "make_call_async", _no_reply)
    return dial_refs


def _fake_hold(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _hold(
        lead_id: str,
        dialled_at: datetime,
        telephony_number_id: str,
        marker: Dict[str, Any],
    ) -> Optional[LeadCallTracker]:
        lead = harness.leads.get(lead_id)
        if not harness.cas_succeeds or lead is None:
            return None
        lead.status = LeadCallStatus.PROCESSING
        lead.call_id = None
        lead.call_initiated_time = dialled_at
        lead.telephony_number_id = telephony_number_id
        lead.metaData = {**(lead.metaData or {}), **marker}
        return lead

    monkeypatch.setattr(w, "hold_unknown_dial", _hold)


async def _dispatch_one(harness: Any, fake_redis: Any, lead: LeadCallTracker) -> None:
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 1)
    await fake_redis.client.rpush(READY_LIST, lead.id)
    await w.Worker(worker_uuid="w-unknown")._iteration(session=None)


async def test_an_unknown_outcome_holds_the_lead_like_a_placed_call(
    harness: Any, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dial_refs = _reply_times_out(harness, monkeypatch)
    _fake_hold(harness, monkeypatch)
    unrecorded: List[Any] = []

    async def _unrecord(self: Any, *args: Any) -> None:
        unrecorded.append(args)

    monkeypatch.setattr(w.Worker, "_unrecord_call_limit", _unrecord)
    lead = make_lead("lead-1")

    await _dispatch_one(harness, fake_redis, lead)

    assert lead.status == LeadCallStatus.PROCESSING
    assert lead.call_id is None
    # Channel and number stay taken, exactly as for a placed call.
    assert await channel_tokens_available(harness.number.id) == 0
    assert harness.released_numbers == []
    # No redial: nothing deferred, the lock stays held, the count stays.
    assert harness.deferred == []
    assert lead.id not in harness.released_locks
    assert unrecorded == []
    # The webhook can match this exact dial.
    (ref,) = dial_refs
    assert ref["lead_id"] == lead.id
    assert datetime.fromisoformat(ref["dial_at"]) == lead.call_initiated_time
    assert (lead.metaData or {})[UNKNOWN_DIAL_META_KEY]["dial_at"] == ref["dial_at"]


async def test_a_lost_hold_releases_everything(
    harness: Any, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row left BACKLOG under us: same as the post-dial CAS loss."""
    _reply_times_out(harness, monkeypatch)
    _fake_hold(harness, monkeypatch)
    harness.cas_succeeds = False
    lead = make_lead("lead-1")

    await _dispatch_one(harness, fake_redis, lead)

    assert await channel_tokens_available(harness.number.id) == 1
    assert harness.released_numbers == [harness.number.id]
    assert lead.id in harness.released_locks


# ---------------------------------------------------------------------------
# Webhook: claim the held lead by lead_id + dial_at
# ---------------------------------------------------------------------------


@pytest.fixture
def claims(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[Any, ...]]:
    seen: List[Tuple[Any, ...]] = []

    async def _claim(*args: Any) -> bool:
        seen.append(args)
        return True

    monkeypatch.setattr(calls_mod, "claim_unknown_dial", _claim)
    return seen


@pytest.mark.parametrize(
    "params",
    [{}, {"lead_id": "lead-1"}, {"lead_id": "lead-1", "dial_at": "yesterday"}],
)
async def test_a_webhook_without_a_readable_dial_ref_touches_nothing(
    claims: List[Tuple[Any, ...]], params: Dict[str, str]
) -> None:
    assert await calls_mod.claim_unknown_dial_from_webhook("CA-1", params) is False
    assert claims == []


async def test_a_webhook_claims_its_exact_dial(
    claims: List[Tuple[Any, ...]],
) -> None:
    assert await calls_mod.claim_unknown_dial_from_webhook("CA-1", DIAL_REF) is True
    assert claims == [("lead-1", DIALLED_AT, "CA-1")]


def _plivo_webhook(path: str, form: Dict[str, str]) -> Request:
    body = urlencode(form).encode()

    async def receive() -> Dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "query_string": urlencode(DIAL_REF).encode(),
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        },
        receive,
    )


async def test_the_hangup_webhook_claims_before_the_lead_is_looked_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: List[str] = []

    async def _claim(call_id: str, params: Any) -> bool:
        order.append(f"claim:{call_id}:{params.get('lead_id')}")
        return True

    async def _record(*args: Any, **kwargs: Any) -> None:
        order.append("lookup")

    monkeypatch.setattr(status_mod, "claim_unknown_dial_from_webhook", _claim)
    monkeypatch.setattr(status_mod, "safe_release_pod", _record)
    monkeypatch.setattr(status_mod, "get_lead_by_call_id", _record)
    monkeypatch.setattr(status_mod, "handle_unanswered_calls", _record)

    request = _plivo_webhook(
        "/plivo/callback/status", {"CallUUID": "CA-1", "CallStatus": "no-answer"}
    )
    await status_mod.handle_callback_status(request, "plivo")

    assert order[0] == "claim:CA-1:lead-1"
    assert "lookup" in order[1:]


async def test_the_answer_webhook_claims_before_the_lead_is_looked_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: List[str] = []

    async def _claim(call_id: str, params: Any) -> bool:
        order.append(f"claim:{call_id}:{params.get('lead_id')}")
        return True

    async def _resolve(*args: Any) -> Dict[str, Any]:
        order.append("resolve")
        return {"error": "stop here", "error_status": 404}

    monkeypatch.setattr(answer_mod, "claim_unknown_dial_from_webhook", _claim)
    monkeypatch.setattr(answer_mod, "resolve_call_templates", _resolve)

    request = _plivo_webhook(
        "/plivo/answer",
        {"CallUUID": "CA-1", "From": "+918000000000", "To": "+919999999999"},
    )
    await answer_mod._handle_provider_answer(request, "plivo")

    assert order == ["claim:CA-1:lead-1", "resolve"]


# ---------------------------------------------------------------------------
# Stuck sweep: no webhook in 10 minutes -> dial the same lead again
# ---------------------------------------------------------------------------


def _stuck_lead(*, held: bool, age_minutes: Optional[float] = None) -> LeadCallTracker:
    dialled_at = (
        DIALLED_AT
        if age_minutes is None
        else datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    )
    return LeadCallTracker(
        id="lead-1",
        reseller_id="breeze",
        template="welcome",
        template_id="tmpl-1",
        merchant_id="merchant-1",
        attempt_count=0,
        next_attempt_at=DIALLED_AT,
        payload={"customer_mobile_number": "+919999999999"},
        metaData=(
            {
                UNKNOWN_DIAL_META_KEY: {
                    "dial_at": dialled_at.isoformat(),
                    "call_limit_member": "lead:nonce-1",
                }
            }
            if held
            else {}
        ),
        status=LeadCallStatus.PROCESSING,
        is_locked=True,
        call_id=None if held else "CA-placed",
        call_initiated_time=dialled_at,
        telephony_number_id="num-1",
        execution_mode=ExecutionMode.TELEPHONY,
        call_direction=CallDirection.OUTBOUND,
    )


class _Sweep:
    """Records what the stuck sweep did to one stuck lead."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, lead: LeadCallTracker):
        self.requeue_wins = True
        self.locked_as: Optional[LeadCallTracker] = None
        self.requeued: List[Any] = []
        self.released: List[str] = []
        self.unrecorded: List[Dict[str, Any]] = []
        self.scheduled: List[str] = []
        self.scheduled_templates: List[Any] = []
        self.closed: List[str] = []
        self.unlocked: List[str] = []

        async def leads(
            status: Any, before: datetime, **kwargs: Any
        ) -> List[LeadCallTracker]:
            # Like the real query: only rows dialled before the cutoff.
            assert lead.call_initiated_time is not None
            return [lead] if lead.call_initiated_time < before else []

        async def lock(*args: Any, **kwargs: Any) -> LeadCallTracker:
            return self.locked_as or lead

        async def requeue(lead_id: str, dialled_at: datetime, key: str) -> bool:
            self.requeued.append((lead_id, dialled_at, key))
            return self.requeue_wins

        async def release(locked: LeadCallTracker) -> None:
            self.released.append(locked.id)

        async def unrecord(**kwargs: Any) -> None:
            self.unrecorded.append(kwargs)

        async def schedule(
            lead_id: str, next_attempt_at: datetime, template_id: Any = None
        ) -> None:
            self.scheduled.append(lead_id)
            self.scheduled_templates.append(template_id)

        async def close(**kwargs: Any) -> None:
            self.closed.append(kwargs["id"])

        async def unlock(lead_id: str) -> None:
            self.unlocked.append(lead_id)

        async def no_config(*args: Any) -> None:
            return None

        monkeypatch.setattr(calls_mod, "get_leads_by_status_and_time_before", leads)
        monkeypatch.setattr(calls_mod, "acquire_lock_on_lead_by_id", lock)
        monkeypatch.setattr(calls_mod, "requeue_unknown_dial", requeue)
        monkeypatch.setattr(calls_mod, "_release_call_resources", release)
        monkeypatch.setattr(calls_mod, "unrecord_call_limit", unrecord)
        monkeypatch.setattr(calls_mod, "schedule_lead", schedule)
        monkeypatch.setattr(calls_mod, "update_lead_call_completion_details", close)
        monkeypatch.setattr(calls_mod, "release_lock_on_lead_by_id", unlock)
        monkeypatch.setattr(calls_mod, "_get_lead_config", no_config)


async def test_an_unclaimed_dial_is_dialled_again_not_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Most templates allow no retry: closing it would mean no call at all."""
    sweep = _Sweep(monkeypatch, _stuck_lead(held=True))

    await calls_mod.reconcile_stuck_processing_leads()

    assert sweep.requeued == [("lead-1", DIALLED_AT, UNKNOWN_DIAL_META_KEY)]
    assert sweep.closed == []
    # Channel back once; the call-limit entry taken back; dialled now.
    assert sweep.released == ["lead-1"]
    assert sweep.unrecorded == [
        {
            "merchant_id": "merchant-1",
            "phone": "+919999999999",
            "member": "lead:nonce-1",
        }
    ]
    assert sweep.scheduled == ["lead-1"]
    # with its template, so a v2 number's lead goes straight to its room
    assert sweep.scheduled_templates == ["tmpl-1"]
    # The requeue unlocked the row; a worker may own it by now.
    assert sweep.unlocked == []


async def test_a_dial_claimed_at_the_last_moment_is_left_to_its_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The webhook won the race: the call is live, nothing is released."""
    sweep = _Sweep(monkeypatch, _stuck_lead(held=True))
    sweep.requeue_wins = False

    await calls_mod.reconcile_stuck_processing_leads()

    assert sweep.released == []
    assert sweep.closed == []
    assert sweep.scheduled == []
    assert sweep.unlocked == ["lead-1"]


async def test_a_stuck_placed_call_is_still_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sweep = _Sweep(monkeypatch, _stuck_lead(held=False))

    await calls_mod.reconcile_stuck_processing_leads()

    assert sweep.requeued == []
    assert sweep.closed == ["lead-1"]
    assert sweep.released == ["lead-1"]


# -- the hold before an UNCLAIMED unknown dial is re-dialled (default 5 min) --


async def test_an_unclaimed_dial_is_redialled_after_the_hold_not_ten_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sweep = _Sweep(monkeypatch, _stuck_lead(held=True, age_minutes=6))

    await calls_mod.reconcile_stuck_processing_leads()

    assert [r[0] for r in sweep.requeued] == ["lead-1"]
    assert sweep.scheduled == ["lead-1"]


async def test_an_unclaimed_dial_younger_than_the_hold_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sweep = _Sweep(monkeypatch, _stuck_lead(held=True, age_minutes=4))

    await calls_mod.reconcile_stuck_processing_leads()

    assert sweep.requeued == []
    assert sweep.released == []
    assert sweep.closed == []


async def test_a_normal_processing_call_keeps_the_ten_minute_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A placed call (call_id set) at 6 minutes is not touched."""
    sweep = _Sweep(monkeypatch, _stuck_lead(held=False, age_minutes=6))

    await calls_mod.reconcile_stuck_processing_leads()

    assert sweep.requeued == []
    assert sweep.closed == []
    assert sweep.released == []
    assert sweep.unlocked == []


async def test_a_claimed_dial_is_never_redialled_by_the_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marker still on the row but a call_id arrived: it is a live call."""
    lead = _stuck_lead(held=True, age_minutes=6)
    lead.call_id = "CA-claimed"
    sweep = _Sweep(monkeypatch, lead)

    await calls_mod.reconcile_stuck_processing_leads()

    assert sweep.requeued == []
    assert sweep.closed == []
    assert sweep.released == []


async def test_the_hold_comes_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(calls_mod, "BB_UNKNOWN_DIAL_HOLD_MINUTES", 8)
    sweep = _Sweep(monkeypatch, _stuck_lead(held=True, age_minutes=6))

    await calls_mod.reconcile_stuck_processing_leads()

    assert sweep.requeued == []

    monkeypatch.setattr(calls_mod, "BB_UNKNOWN_DIAL_HOLD_MINUTES", 5)
    await calls_mod.reconcile_stuck_processing_leads()

    assert [r[0] for r in sweep.requeued] == ["lead-1"]


def test_the_hold_defaults_to_five_minutes() -> None:
    assert static.BB_UNKNOWN_DIAL_HOLD_MINUTES == 5


async def test_a_lead_claimed_between_select_and_lock_is_not_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selected as unclaimed at 6 min; its webhook claims it before the lock."""
    sweep = _Sweep(monkeypatch, _stuck_lead(held=True, age_minutes=6))
    claimed = _stuck_lead(held=True, age_minutes=6)
    claimed.call_id = "CA-claimed"
    sweep.locked_as = claimed

    await calls_mod.reconcile_stuck_processing_leads()

    assert sweep.requeued == []
    assert sweep.closed == []
    assert sweep.released == []
    assert sweep.unlocked == ["lead-1"]


@pytest.mark.parametrize("configured", [0, -3])
async def test_the_hold_is_at_least_one_minute(
    monkeypatch: pytest.MonkeyPatch, configured: int
) -> None:
    monkeypatch.setattr(calls_mod, "BB_UNKNOWN_DIAL_HOLD_MINUTES", configured)
    young = _Sweep(monkeypatch, _stuck_lead(held=True, age_minutes=0.5))
    await calls_mod.reconcile_stuck_processing_leads()
    assert young.requeued == []

    old = _Sweep(monkeypatch, _stuck_lead(held=True, age_minutes=2))
    await calls_mod.reconcile_stuck_processing_leads()
    assert [r[0] for r in old.requeued] == ["lead-1"]


# ---------------------------------------------------------------------------
# dial_at travels without a '+' (review #1280, finding 5)
# ---------------------------------------------------------------------------


def test_dial_at_travels_in_utc_z_form_and_reads_back_to_the_same_instant() -> None:
    sent = dial_ref_time(DIALLED_AT)

    assert sent == "2026-10-02T04:32:04.123456Z"
    assert "+" not in sent
    # through the webhook URL and back: the exact instant the hold was written at
    echoed = parse_qs(urlencode({"dial_at": sent}))["dial_at"][0]
    assert datetime.fromisoformat(echoed) == DIALLED_AT


async def test_the_worker_sends_dial_at_without_a_plus(
    harness: Any, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dial_refs = _reply_times_out(harness, monkeypatch)
    _fake_hold(harness, monkeypatch)
    lead = make_lead("lead-1")

    await _dispatch_one(harness, fake_redis, lead)

    (ref,) = dial_refs
    assert ref["dial_at"].endswith("Z") and "+" not in ref["dial_at"]
    assert datetime.fromisoformat(ref["dial_at"]) == lead.call_initiated_time


async def test_a_webhook_with_a_z_dial_at_claims_its_exact_dial(
    claims: List[Tuple[Any, ...]],
) -> None:
    ref = {"lead_id": "lead-1", "dial_at": dial_ref_time(DIALLED_AT)}

    assert await calls_mod.claim_unknown_dial_from_webhook("CA-1", ref) is True
    assert claims == [("lead-1", DIALLED_AT, "CA-1")]


async def test_a_dial_at_whose_plus_became_a_space_is_refused_without_crashing(
    claims: List[Tuple[Any, ...]],
) -> None:
    mangled = {"lead_id": "lead-1", "dial_at": "2026-10-02T04:32:04.123456 00:00"}

    assert await calls_mod.claim_unknown_dial_from_webhook("CA-1", mangled) is False
    assert claims == []


# ---------------------------------------------------------------------------
# Plivo: "sent, no reply" is unknown; only "never connected" is not placed
# (review #1280, finding 7)
# ---------------------------------------------------------------------------


def _connection_dropped_before_the_reply() -> requests.exceptions.ConnectionError:
    # what requests raises when Plivo closes the socket after the request went out
    cause = urllib3.exceptions.ProtocolError(
        "Connection aborted.",
        http.client.RemoteDisconnected("Remote end closed connection without response"),
    )
    return requests.exceptions.ConnectionError(cause)


def _never_connected(reason: Exception) -> requests.exceptions.ConnectionError:
    return requests.exceptions.ConnectionError(
        urllib3.exceptions.MaxRetryError(
            cast(Any, None), "/v1/Account/x/Call/", reason=reason
        )
    )


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.ReadTimeout("read timeout=15"),
        _connection_dropped_before_the_reply(),
        requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead"),
        requests.exceptions.ConnectionError(ConnectionResetError(104, "reset")),
    ],
    ids=["read-timeout", "dropped-before-reply", "broken-body", "reset-unclassified"],
)
def test_a_dial_sent_without_a_usable_reply_is_unknown_never_none(
    plivo: PlivoProvider, error: Exception
) -> None:
    _plivo_returns(plivo, error)

    assert plivo.make_call("+919999999999", "+918000000000", dial_ref=DIAL_REF) == {
        "status": DIAL_OUTCOME_UNKNOWN,
        "sid": None,
    }


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.ConnectTimeout("connect timeout"),
        _never_connected(
            urllib3.exceptions.NewConnectionError(
                cast(Any, None),
                "Failed to establish a new connection: [Errno 111] refused",
            )
        ),
        _never_connected(
            urllib3.exceptions.NameResolutionError(
                "api.plivo.com", cast(Any, None), socket.gaierror(-2, "Name unknown")
            )
        ),
    ],
    ids=["connect-timeout", "refused", "dns"],
)
def test_a_dial_that_never_connected_is_not_placed(
    plivo: PlivoProvider, error: Exception
) -> None:
    _plivo_returns(plivo, error)

    assert plivo.make_call("+919999999999", "+918000000000", dial_ref=DIAL_REF) is None


def test_a_non_transport_error_before_any_reply_is_raised(
    plivo: PlivoProvider,
) -> None:
    """Nothing reached Plivo: make_call raises and the caller's failure path treats the
    dial as not placed (spec 2026-10-05 §10.3)."""
    _plivo_returns(plivo, ValueError("bad number"))

    with pytest.raises(ValueError):
        plivo.make_call("+919999999999", "+918000000000", dial_ref=DIAL_REF)
