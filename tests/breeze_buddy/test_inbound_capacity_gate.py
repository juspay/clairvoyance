"""
Tests for the inbound Plivo channel capacity gate.

The DB ceiling itself is Postgres's contract (a single atomic
``UPDATE ... WHERE channels < maximum_channels``); what needs proving here is
the Python side of the bargain: that every admitted inbound call returns
exactly one channel, and that a call which never took one never returns one.
Over-releasing invents capacity the trunk does not have.
"""

from __future__ import annotations

import os
from typing import Any, List, Optional, Tuple, cast

import pytest
from fastapi import WebSocket
from starlette.responses import HTMLResponse

# dispatch must import first: managers.calls and dispatch.worker import each
# other, and only this order resolves it (the order the app itself loads them
# in). "...breeze_buddy" sorts before "...breeze_buddy.managers", so isort
# keeps this line above the next one on its own.
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.managers import (
    calls as calls_mod,
    inbound_channel as ic_mod,
)
from app.ai.voice.agents.breeze_buddy.services.inbound_policy import (
    PolicyResult,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo import (
    plivo as plivo_mod,
)
from app.api.routers.breeze_buddy.telephony.answer import handlers as ans_mod
from app.database.queries.breeze_buddy.dispatch import (
    count_processing_by_telephony_number_query,
)
from app.schemas import CallDirection, CallProvider, ExecutionMode, LeadCallStatus
from app.schemas.breeze_buddy.core import (
    LeadCallTracker,
    TelephonyNumber,
    TelephonyNumberStatus,
)


def make_inbound_lead(
    status: LeadCallStatus = LeadCallStatus.PROCESSING,
    number_id: Optional[str] = "num-1",
    outcome: Optional[str] = None,
) -> LeadCallTracker:
    return LeadCallTracker(
        id="lead-in-1",
        telephony_number_id=number_id,
        reseller_id="res-1",
        template="support",
        merchant_id="merchant-1",
        status=status,
        outcome=outcome,
        call_id="CALL-1",
        call_direction=CallDirection.INBOUND,
        execution_mode=ExecutionMode.TELEPHONY,
    )


def make_outbound_lead(
    status: LeadCallStatus = LeadCallStatus.PROCESSING,
    execution_mode: ExecutionMode = ExecutionMode.TELEPHONY,
) -> LeadCallTracker:
    return LeadCallTracker(
        id="lead-out-1",
        telephony_number_id="num-1",
        reseller_id="res-1",
        template="welcome",
        status=status,
        call_direction=CallDirection.OUTBOUND,
        execution_mode=execution_mode,
    )


def make_number(provider: CallProvider = CallProvider.PLIVO) -> TelephonyNumber:
    return TelephonyNumber(
        id="num-1",
        number="+15551230000",
        provider=provider,
        status=TelephonyNumberStatus.AVAILABLE,
        channels=1,
        maximum_channels=5,
    )


class ReleaseSpy:
    """Records what _release_call_resources actually did."""

    def __init__(self, number: Optional[TelephonyNumber]) -> None:
        self.number = number
        self.released: List[Tuple[str, CallProvider]] = []
        self.tokens: List[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "ReleaseSpy":
        async def fake_get(number_id: str):
            return self.number

        async def fake_release_number(number_id: str, provider: CallProvider):
            self.released.append((number_id, provider))

        async def fake_release_token(number_id: str, token=None):
            self.tokens.append(number_id)
            return True

        async def fake_decrement(number_id: str):
            self.released.append((number_id, CallProvider.PLIVO))

        monkeypatch.setattr(calls_mod, "get_telephony_number_by_id", fake_get)
        monkeypatch.setattr(calls_mod, "_release_number", fake_release_number)
        monkeypatch.setattr(calls_mod, "release_channel_token", fake_release_token)
        # Inbound goes through the shared module; patch its collaborators (not
        # release_inbound_channel itself) so inbound_holds_channel stays in
        # the loop — that predicate is the logic under test.
        monkeypatch.setattr(ic_mod, "get_telephony_number_by_id", fake_get)
        monkeypatch.setattr(
            ic_mod, "decrement_telephony_number_channels", fake_decrement
        )
        return self


# ── the release predicate ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "provider,status,expected",
    [
        (CallProvider.PLIVO, LeadCallStatus.PROCESSING, True),
        (CallProvider.PLIVO, LeadCallStatus.FINISHED, False),
        (CallProvider.PLIVO, LeadCallStatus.BACKLOG, False),
        # Only Plivo inbound is gated, so only Plivo inbound owes a channel.
        (CallProvider.EXOTEL, LeadCallStatus.PROCESSING, False),
        (CallProvider.TWILIO, LeadCallStatus.PROCESSING, False),
    ],
)
def test_inbound_releases_only_when_it_acquired(provider, status, expected):
    lead = make_inbound_lead(status=status)
    assert calls_mod._releases_capacity(lead, provider) is expected


@pytest.mark.parametrize(
    "execution_mode,expected",
    [
        (ExecutionMode.TELEPHONY, True),
        (ExecutionMode.TELEPHONY_TEST, True),
        (ExecutionMode.DAILY, False),
        (ExecutionMode.DAILY_TEST, False),
    ],
)
def test_outbound_release_unchanged_by_the_refactor(execution_mode, expected):
    """Outbound keys off execution mode, never off status or provider."""
    for status in (LeadCallStatus.PROCESSING, LeadCallStatus.FINISHED):
        lead = make_outbound_lead(status=status, execution_mode=execution_mode)
        assert calls_mod._releases_capacity(lead, CallProvider.PLIVO) is expected


# ── the release path ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inbound_release_returns_channel_but_never_a_redis_token(monkeypatch):
    """Inbound never takes a dispatch token, so it must not push one back —
    that would hand outbound capacity that was never borrowed."""
    spy = ReleaseSpy(make_number()).install(monkeypatch)
    await calls_mod._release_call_resources(make_inbound_lead())
    assert spy.released == [("num-1", CallProvider.PLIVO)]
    assert spy.tokens == []


@pytest.mark.asyncio
async def test_outbound_release_returns_both_channel_and_token(monkeypatch):
    spy = ReleaseSpy(make_number()).install(monkeypatch)
    await calls_mod._release_call_resources(make_outbound_lead())
    assert spy.released == [("num-1", CallProvider.PLIVO)]
    assert spy.tokens == ["num-1"]


@pytest.mark.asyncio
async def test_duplicate_inbound_callback_releases_exactly_once(monkeypatch):
    """Two webhooks for one call: the first sees PROCESSING, the second sees
    the FINISHED row it wrote. Only one channel goes back."""
    spy = ReleaseSpy(make_number()).install(monkeypatch)
    await calls_mod._release_call_resources(
        make_inbound_lead(LeadCallStatus.PROCESSING)
    )
    await calls_mod._release_call_resources(make_inbound_lead(LeadCallStatus.FINISHED))
    assert len(spy.released) == 1


@pytest.mark.asyncio
async def test_capacity_rejected_call_never_releases(monkeypatch):
    """A call turned away at the gate wrote a FINISHED row and never held a
    channel. Releasing for it would invent capacity."""
    spy = ReleaseSpy(make_number()).install(monkeypatch)
    lead = make_inbound_lead(
        status=LeadCallStatus.FINISHED,
        outcome=ans_mod.CAPACITY_REJECTED_OUTCOME,
    )
    await calls_mod._release_call_resources(lead)
    assert spy.released == []


@pytest.mark.asyncio
async def test_release_is_a_noop_without_a_telephony_number(monkeypatch):
    spy = ReleaseSpy(None).install(monkeypatch)
    await calls_mod._release_call_resources(make_inbound_lead(number_id=None))
    assert spy.released == []


@pytest.mark.asyncio
async def test_release_survives_a_vanished_number_row(monkeypatch):
    spy = ReleaseSpy(None).install(monkeypatch)
    await calls_mod._release_call_resources(make_inbound_lead())
    assert spy.released == []


# ── the admission gate ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admission_fails_closed_when_the_db_says_no(monkeypatch):
    """The accessor collapses 'at capacity' and 'UPDATE blew up' into None,
    so both reject the caller."""

    async def at_capacity(number_id: str):
        return None

    monkeypatch.setattr(plivo_mod, "increment_telephony_number_channels", at_capacity)
    assert await plivo_mod.admit_plivo_inbound_call("num-1") is False


@pytest.mark.asyncio
async def test_admission_succeeds_when_a_row_comes_back(monkeypatch):
    async def admitted(number_id: str):
        return make_number()

    monkeypatch.setattr(plivo_mod, "increment_telephony_number_channels", admitted)
    assert await plivo_mod.admit_plivo_inbound_call("num-1") is True


@pytest.mark.asyncio
async def test_failed_lead_insert_is_reported_so_the_channel_can_be_refunded(
    monkeypatch,
):
    """No PROCESSING row means nothing downstream will ever decrement. The
    bool is the only signal the handler has to hand the channel back."""

    class _T:
        id = "tmpl-1"
        name = "support"
        reseller_id = "res-1"
        merchant_id = "merchant-1"
        telephony_number_id = "num-1"

    async def insert_returns_none(**kwargs):
        return None

    async def insert_raises(**kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(ans_mod, "create_lead_call_tracker", insert_returns_none)
    assert (
        await ans_mod._create_inbound_lead_in_answer_handler(
            call_id="CALL-1", from_number="+15550001111", templates=[_T()]
        )
        is False
    )

    monkeypatch.setattr(ans_mod, "create_lead_call_tracker", insert_raises)
    assert (
        await ans_mod._create_inbound_lead_in_answer_handler(
            call_id="CALL-1", from_number="+15550001111", templates=[_T()]
        )
        is False
    )


@pytest.mark.asyncio
async def test_no_templates_reports_no_channel_owed(monkeypatch):
    assert (
        await ans_mod._create_inbound_lead_in_answer_handler(
            call_id="CALL-1", from_number="+15550001111", templates=[]
        )
        is False
    )


# ── the reconciler's view of held capacity ───────────────────────────────


# (lead_id, provider, direction, status, execution_mode, holds_a_channel)
HELD_BY = [
    ("a", "PLIVO", "INBOUND", "PROCESSING", "TELEPHONY", True),
    ("b", "PLIVO", "OUTBOUND", "PROCESSING", "TELEPHONY", True),
    ("c", "PLIVO", "OUTBOUND", "PROCESSING", "TELEPHONY_TEST", True),
    ("d", "PLIVO", "INBOUND", "FINISHED", "TELEPHONY", False),
    ("e", "PLIVO", "OUTBOUND", "PROCESSING", "DAILY", False),
    ("f", "PLIVO", "OUTBOUND", "BACKLOG", "TELEPHONY", False),
    ("g", "EXOTEL", "INBOUND", "PROCESSING", "TELEPHONY", False),
    ("h", "EXOTEL", "OUTBOUND", "PROCESSING", "TELEPHONY", True),
    ("i", "TWILIO", "INBOUND", "PROCESSING", "TELEPHONY", False),
    ("j", "TWILIO", "OUTBOUND", "PROCESSING", "TELEPHONY", True),
]


@pytest.mark.parametrize("_lid,provider,direction,status,_mode,held", HELD_BY)
def test_release_predicate_over_every_inbound_shape(
    _lid, provider, direction, status, _mode, held
):
    """The inbound half of the matched pair, checked directly.

    The other half — that the SQL counts exactly these rows — cannot be
    asserted honestly without a database: substring checks on the query text
    pass even when the whole predicate is negated. That half lives in
    ``test_in_flight_count_against_postgres`` below, which runs the real SQL.
    """
    if direction != "INBOUND":
        pytest.skip("outbound release keys off execution mode, not the row")

    lead = make_inbound_lead(status=LeadCallStatus(status))
    assert calls_mod._releases_capacity(lead, CallProvider(provider)) is held


@pytest.mark.asyncio
async def test_in_flight_count_against_postgres():
    """Run the REAL count query against a REAL database over every lead shape.

    Skipped unless CAPGATE_TEST_DSN points at a throwaway Postgres, so this
    does NOT protect CI today — it is the local/staging guard. It exists
    because the thing it checks cannot be checked any other way: assertions on
    SQL text still pass with the predicate fully inverted, and the count
    driving the token stock is exactly the kind of logic that fails silently.

        podman run -d -e POSTGRES_PASSWORD=t -e POSTGRES_USER=t \
            -e POSTGRES_DB=t -p 55440:5432 docker.io/library/postgres:16-alpine
        CAPGATE_TEST_DSN=postgresql://t:t@localhost:55440/t uv run pytest ...
    """
    dsn = os.environ.get("CAPGATE_TEST_DSN")
    if not dsn:
        pytest.skip("set CAPGATE_TEST_DSN to run the count query against Postgres")

    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DROP TABLE IF EXISTS lead_call_tracker, telephony_numbers")
        await conn.execute("""CREATE TABLE telephony_numbers (
                id VARCHAR(255) PRIMARY KEY, number VARCHAR(20) NOT NULL,
                provider VARCHAR(50) NOT NULL, status VARCHAR(50) NOT NULL,
                channels INTEGER NOT NULL DEFAULT 0,
                maximum_channels INTEGER NOT NULL DEFAULT 1)""")
        await conn.execute("""CREATE TABLE lead_call_tracker (
                id VARCHAR(255) PRIMARY KEY, telephony_number_id VARCHAR(255),
                status VARCHAR(50), call_direction VARCHAR(20),
                execution_mode VARCHAR(50))""")
        for provider in ("PLIVO", "EXOTEL", "TWILIO"):
            await conn.execute(
                "INSERT INTO telephony_numbers (id,number,provider,status) "
                "VALUES ($1,$2,$3,'AVAILABLE')",
                f"n-{provider}",
                f"+1555{provider[:4]}",
                provider,
            )
        for lid, provider, direction, status, mode, _ in HELD_BY:
            await conn.execute(
                "INSERT INTO lead_call_tracker VALUES ($1,$2,$3,$4,$5)",
                lid,
                f"n-{provider}",
                status,
                direction,
                mode,
            )

        text, values = count_processing_by_telephony_number_query()
        rows = await conn.fetch(text, *values)
        counted = {r["telephony_number_id"]: r["in_flight"] for r in rows}

        expected: dict = {}
        for _lid, provider, _d, _s, _m, held in HELD_BY:
            if held:
                expected[f"n-{provider}"] = expected.get(f"n-{provider}", 0) + 1

        assert counted == expected, (
            f"the count query does not select the channel holders: "
            f"got {counted}, expected {expected}"
        )
    finally:
        await conn.execute("DROP TABLE IF EXISTS lead_call_tracker, telephony_numbers")
        await conn.close()


# ── ordering guard ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inbound_release_depends_on_a_pre_update_snapshot(monkeypatch):
    """Regression guard for a trap in the refactor.

    Inbound release is decided by reading ``lead.status`` off an in-memory
    object. Both call sites work only because they pass a snapshot taken
    while the lead was still PROCESSING — ``handle_call_completion`` releases
    before its UPDATE, and the stuck sweep passes the pre-update copy. Refetch
    the lead first and inbound silently stops returning channels, which
    strangles every number one call at a time. This test fails if someone
    'tidies' that ordering away.
    """
    spy = ReleaseSpy(make_number()).install(monkeypatch)

    pre_update = make_inbound_lead(status=LeadCallStatus.PROCESSING)
    await calls_mod._release_call_resources(pre_update)
    assert len(spy.released) == 1, "a PROCESSING snapshot must return its channel"

    post_update = make_inbound_lead(status=LeadCallStatus.FINISHED)
    await calls_mod._release_call_resources(post_update)
    assert len(spy.released) == 1, (
        "a refetched (already FINISHED) lead releases nothing — release must "
        "run before the completion UPDATE, not after"
    )


# ── the IVR deferred-policy block ────────────────────────────────────────


class _FakeWS:
    """Minimal stand-in: the block path only closes the socket."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self, *args, **kwargs) -> None:
        self.closed = True


async def _drive_ivr_block(
    monkeypatch,
    provider: CallProvider,
    action: str = "REJECT",
    redirect_ok: bool = False,
    capture: Optional[dict] = None,
    claim_lost: bool = False,
) -> List[str]:
    """Run the IVR deferred-policy block path to completion and report the
    order of the two writes that matter."""
    from app.ai.voice.agents.breeze_buddy.ivr import selection as ivr_mod

    order: List[str] = []
    number = make_number(provider=provider)

    class _Template:
        id = "tmpl-1"
        name = "support"
        reseller_id = "res-1"
        merchant_id = "merchant-1"
        telephony_number_id = "num-1"
        configurations = None

    class _Config:
        pass

    lead = make_inbound_lead(status=LeadCallStatus.PROCESSING)

    async def fake_get_template(_id):
        return _Template()

    async def fake_get_config(_id):
        return _Config()

    async def fake_policy(*args, **kwargs):
        # Blocked — e.g. this caller is over the selected template's rate limit.
        return PolicyResult(
            allowed=False,
            action=getattr(ivr_mod.InboundBlockAction, action),
            message=None,
            redirect_number="+15559998888" if action == "REDIRECT" else None,
            reason="rate_limit_exceeded",
        )

    async def fake_get_lead(_call_sid):
        return lead

    async def fake_get_number(_number_id):
        return number

    async def fake_release_number(number_id):
        order.append("released_channel")

    async def fake_release_token(number_id, token=None):
        return True

    async def fake_completion(**kwargs):
        order.append(f"finished:{kwargs.get('status')}")
        if capture is not None:
            capture.update(kwargs)
        return None if claim_lost else lead

    async def fake_update_template(**kwargs):
        return None

    async def fake_redirect_call(**kwargs):
        order.append("redirected")
        return redirect_ok

    monkeypatch.setattr(ivr_mod, "redirect_call", fake_redirect_call)
    monkeypatch.setattr(ivr_mod, "get_template_by_id", fake_get_template)
    monkeypatch.setattr(
        ivr_mod, "get_call_execution_config_by_template_id", fake_get_config
    )
    monkeypatch.setattr(ivr_mod, "check_inbound_policy", fake_policy)
    monkeypatch.setattr(ivr_mod, "get_lead_by_call_id", fake_get_lead)
    monkeypatch.setattr(ivr_mod, "update_lead_call_completion_details", fake_completion)
    monkeypatch.setattr(ivr_mod, "update_lead_template", fake_update_template)
    # Patch the shared module's collaborators, not release_inbound_channel
    # itself, so inbound_holds_channel stays in the loop — that predicate is
    # the logic under test.
    monkeypatch.setattr(ic_mod, "get_telephony_number_by_id", fake_get_number)
    monkeypatch.setattr(
        ic_mod, "decrement_telephony_number_channels", fake_release_number
    )

    blocked = await ivr_mod._check_deferred_inbound_policy(
        # The block path only closes the socket; cast keeps the stub honest.
        ws=cast(WebSocket, _FakeWS()),
        stream_sid="stream-1",
        template_id="tmpl-1",
        from_number="+15550001111",
        provider="plivo",
        telephony_service=cast(Any, object()) if action == "REDIRECT" else None,
        call_sid="CALL-1",
    )
    assert blocked is True
    return order


@pytest.mark.asyncio
async def test_ivr_block_returns_the_channel_before_finishing_the_lead(monkeypatch):
    """A caller blocked after picking an IVR option still holds the channel the
    answer-time gate took. The row is the receipt, so it must be refunded
    before the row is finished — afterwards _releases_capacity sees FINISHED,
    the call-end webhook correctly refunds nothing, the stuck sweep only scans
    PROCESSING, and nothing reconciles telephony_numbers.channels. The channel
    would be lost for good.
    """
    order = await _drive_ivr_block(monkeypatch, CallProvider.PLIVO)

    assert "released_channel" in order, (
        "IVR block finished the lead without returning its channel — the "
        "number permanently loses one channel per blocked caller"
    )
    # Finish first, release second: the refund is conditional on the UPDATE
    # actually transitioning the row, so a silently failed UPDATE cannot
    # produce a second release from the websocket teardown path.
    assert order.index(f"finished:{LeadCallStatus.FINISHED}") < order.index(
        "released_channel"
    ), "release must be gated on a confirmed completion UPDATE"


@pytest.mark.asyncio
async def test_ivr_block_on_an_ungated_provider_returns_nothing(monkeypatch):
    """Exotel inbound never took a channel, so the same path must not invent
    one by refunding."""
    order = await _drive_ivr_block(monkeypatch, CallProvider.EXOTEL)
    assert "released_channel" not in order


# ── the failed-insert path must not refund ───────────────────────────────


def test_failed_insert_path_does_not_refund_the_channel():
    """A failed answer-time insert does NOT end the call. The request falls
    through to the provider response, the caller connects, and the websocket
    creates its own PROCESSING lead on the same number
    (agent/inbound.py::create_lead_from_template_id) which then owns and
    releases the channel. Refunding here would make that a second release for
    one acquire — an under-count, which over-admits past maximum_channels.

    Asserted structurally: the answer handler must not hold a decrement at
    all. The genuinely-stranded case (caller hangs up before the stream
    opens) is repaired by reconcile_channel_tokens instead.
    """
    import inspect

    source = inspect.getsource(ans_mod)
    # Cover BOTH ways the refund could come back: the raw accessor and the
    # shared helper. It cannot catch a refund written some third way, which is
    # why the drift table lives in a comment at the call site.
    for forbidden in ("decrement_telephony_number_channels", "release_inbound_channel"):
        assert forbidden not in source, (
            f"the answer handler calls {forbidden}; the websocket-created lead "
            "will also release it — see the drift table in the gate's comment"
        )


@pytest.mark.asyncio
async def test_websocket_created_lead_owns_the_channel(monkeypatch):
    """The lead the websocket falls back to creating carries exactly the
    fields that make it release the channel: INBOUND, PROCESSING, and the same
    telephony number. This is what makes not-refunding correct."""
    from app.ai.voice.agents.breeze_buddy.agent import inbound as ws_inbound

    captured: dict = {}

    class _Template:
        id = "tmpl-1"
        name = "support"
        reseller_id = "res-1"
        merchant_id = "merchant-1"
        telephony_number_id = "num-1"

    async def no_existing_lead(_call_sid):
        return None

    async def fake_get_template(_id):
        return _Template()

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return make_inbound_lead()

    monkeypatch.setattr(ws_inbound, "get_lead_by_call_id", no_existing_lead)
    monkeypatch.setattr(ws_inbound, "get_template_by_id", fake_get_template)
    monkeypatch.setattr(ws_inbound, "create_lead_call_tracker", fake_create)

    from datetime import datetime, timezone

    await ws_inbound.create_lead_from_template_id(
        template_id="tmpl-1",
        call_sid="CALL-1",
        call_data={},
        call_initiated_time=datetime.now(timezone.utc),
    )

    assert captured["call_direction"] == CallDirection.INBOUND
    assert captured["status"] == LeadCallStatus.PROCESSING
    assert captured["telephony_number_id"] == "num-1"
    # Which means _releases_capacity will refund it exactly once at call end.
    lead = make_inbound_lead(status=LeadCallStatus.PROCESSING)
    assert calls_mod._releases_capacity(lead, CallProvider.PLIVO) is True


# ── the dynamic INSERT column list ───────────────────────────────────────


def _columns_and_values(text: str, values: list) -> dict:
    """Pair the emitted column list with the bound values, proving the $n
    tokens are contiguous and that there is exactly one per value."""
    import re

    match = re.search(r"\(([^()]*?)\)\s*VALUES", text, re.S)
    assert match is not None, f"could not find the column list in:\n{text}"
    column_block = match.group(1)
    columns = [c.strip().strip('"') for c in column_block.split(",") if c.strip()]
    placeholders = [int(x) for x in re.findall(r"\$(\d+)", text)]
    assert placeholders == list(
        range(1, len(values) + 1)
    ), f"placeholders {placeholders} are not $1..${len(values)}"
    assert len(columns) == len(
        values
    ), f"{len(columns)} columns but {len(values)} values"
    return dict(zip(columns, values))


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({}, {}),
        ({"channels": 0}, {"channels": 0}),
        ({"maximum_channels": 7}, {"maximum_channels": 7}),
        (
            {"channels": 2, "maximum_channels": 9},
            {"channels": 2, "maximum_channels": 9},
        ),
    ],
)
def test_insert_binds_every_column_to_its_own_value(kwargs, expected):
    """Since migration 076 the capacity columns are NOT NULL with defaults, and
    a DEFAULT only fires when the column is absent from the INSERT — binding
    NULL would break admin create-number, which leaves maximum_channels unset.

    Building the column list dynamically introduces exactly one bug class:
    columns and values drifting out of alignment. So assert the binding itself.
    A placeholder count still passes when every value is shifted by one or two
    columns are swapped; this does not.
    """
    from app.database.queries.breeze_buddy.telephony_number import (
        insert_telephony_number_query,
    )

    text, values = insert_telephony_number_query(
        "id-1",
        "+15551230000",
        CallProvider.PLIVO,
        TelephonyNumberStatus.AVAILABLE,
        reseller_id="res-1",
        merchant_id="merchant-1",
        **kwargs,
    )
    bound = _columns_and_values(text, values)

    assert bound["id"] == "id-1"
    assert bound["number"] == "+15551230000"
    assert bound["provider"] == CallProvider.PLIVO.value
    assert bound["status"] == TelephonyNumberStatus.AVAILABLE.value
    assert bound["reseller_id"] == "res-1"
    assert bound["merchant_id"] == "merchant-1"

    for column in ("channels", "maximum_channels"):
        if column in expected:
            assert bound[column] == expected[column]
        else:
            assert column not in bound, f"{column} bound as NULL instead of omitted"


# ── the atomic completion claim ──────────────────────────────────────────


def test_completion_update_without_expected_status_is_not_a_claim():
    """Documents the trap this fix exists for: the plain UPDATE keys on the id
    alone, so a returned row means "this row exists", not "I won the
    transition". Anything deciding who owes a side effect must not use it."""
    from app.database.queries.breeze_buddy.lead_call_tracker import (
        update_lead_call_completion_details_query,
    )

    text, values = update_lead_call_completion_details_query(
        "lead-1", status=LeadCallStatus.FINISHED
    )
    where = text.split("WHERE")[1].split("RETURNING")[0]
    assert '"status"' not in where
    assert len(values) == 2


def test_completion_update_with_expected_status_is_an_atomic_claim():
    """With expected_status the row is only written while it still holds that
    status, so a returned row means THIS caller performed the transition."""
    from app.database.queries.breeze_buddy.lead_call_tracker import (
        update_lead_call_completion_details_query,
    )

    text, values = update_lead_call_completion_details_query(
        "lead-1",
        status=LeadCallStatus.FINISHED,
        expected_status=LeadCallStatus.PROCESSING,
    )
    where = text.split("WHERE")[1].split("RETURNING")[0]
    assert '"id" = $' in where
    assert '"status" = $' in where
    assert values[-1] == LeadCallStatus.PROCESSING.value
    # The guard must be in WHERE, not SET — writing it there would be a no-op.
    set_block = text.split("SET")[1].split("WHERE")[0]
    assert set_block.count('"status"') == 1, "status should be SET exactly once"
    assert values[-1] == LeadCallStatus.PROCESSING.value
    assert where.count("AND") == 1


@pytest.mark.asyncio
async def test_ivr_block_claims_before_releasing(monkeypatch):
    """The IVR release must be gated on winning the transition, not on the row
    existing — two callbacks that both merely 'updated the row' would both
    refund the same channel."""
    seen: dict = {}
    order = await _drive_ivr_block(monkeypatch, CallProvider.PLIVO, capture=seen)

    assert seen.get("expected_status") == LeadCallStatus.PROCESSING, (
        "the IVR completion write is not an atomic claim, so a racing callback "
        "could refund the same channel twice"
    )
    assert "released_channel" in order


# ── the gate's real call site ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_answer_handler_admits_and_rejects_at_the_real_call_site(monkeypatch):
    """Drive _handle_provider_answer itself, both outcomes.

    Every other test here exercises the gate's pieces in isolation, which
    leaves the feature deletable with a green build: short-circuiting the
    whole admission block, or dropping the busy response, changed nothing.
    This asserts the wiring — that the gate is consulted on the real path and
    that a refusal actually reaches the caller.
    """
    admitted_calls: List[str] = []
    rejected: List[str] = []

    class _Template:
        id = "tmpl-1"
        name = "support"
        reseller_id = "res-1"
        merchant_id = "merchant-1"
        telephony_number_id = "num-1"
        configurations = None

    number = make_number(provider=CallProvider.PLIVO)

    async def fake_resolve(call_sid, from_number, to_number):
        return {
            "is_outbound": False,
            "templates": [_Template()],
            "template_list": [{"id": "tmpl-1", "name": "support"}],
            "reseller_id": "res-1",
            "telephony_number": number,
            "ivr_greeting": None,
            "ivr_goodbye": None,
            "ivr_voice_config": None,
        }

    async def no_config(_template_id):
        return None

    async def fake_create_lead(**kwargs):
        return True

    async def fake_response(provider, result, call_id, from_number, to_number):
        admitted_calls.append(call_id)
        return HTMLResponse(content="<Response/>", media_type="application/xml")

    monkeypatch.setattr(ans_mod, "resolve_call_templates", fake_resolve)
    monkeypatch.setattr(ans_mod, "get_call_execution_config_by_template_id", no_config)
    monkeypatch.setattr(
        ans_mod, "_create_inbound_lead_in_answer_handler", fake_create_lead
    )
    monkeypatch.setattr(ans_mod, "_build_provider_response", fake_response)
    monkeypatch.setattr(ans_mod, "spawn_background_task", lambda *a, **k: None)

    class _Request:
        method = "POST"

        def __init__(self, data: dict) -> None:
            self._data = data

        async def form(self):
            return self._data

        @property
        def query_params(self):
            return {}

    request = cast(
        Any,
        _Request({"CallUUID": "CALL-1", "From": "+15550001111", "To": "+15551230000"}),
    )

    # 1. capacity available -> the call is served
    async def admits(_number_id):
        return number

    monkeypatch.setattr(plivo_mod, "increment_telephony_number_channels", admits)
    await ans_mod._handle_provider_answer(request, "plivo")
    assert admitted_calls == ["CALL-1"], "the gate refused a call it had room for"

    # 2. at capacity -> the caller gets the busy response, NOT the agent
    async def at_capacity(_number_id):
        return None

    monkeypatch.setattr(plivo_mod, "increment_telephony_number_channels", at_capacity)
    response = await ans_mod._handle_provider_answer(request, "plivo")
    body = bytes(response.body).decode()

    assert admitted_calls == ["CALL-1"], "a call was served despite no free channel"
    assert "Speak" in body and "Hangup" in body, f"not the busy response: {body}"
    assert ans_mod._INBOUND_CAPACITY_MESSAGE.split(",")[0] in body
    rejected.append("CALL-1")
    assert rejected


@pytest.mark.asyncio
async def test_ivr_does_not_release_when_it_loses_the_claim(monkeypatch):
    """Losing the claim means another caller already transitioned the lead and
    already returned the channel. Releasing anyway is the double-refund the
    claim exists to prevent — an under-count, which over-admits."""
    order = await _drive_ivr_block(monkeypatch, CallProvider.PLIVO, claim_lost=True)
    assert (
        "released_channel" not in order
    ), "released a channel after losing the claim — the winner already did"


@pytest.mark.asyncio
async def test_accessor_forwards_expected_status_to_the_query(monkeypatch):
    """The claim is only atomic if the accessor actually passes it down. If it
    silently drops the argument every claim degrades to a plain UPDATE and the
    whole exactly-once property goes with it — with no visible symptom."""
    from app.database.accessor.breeze_buddy import lead_call_tracker as acc

    seen: dict = {}

    def spy_query(
        id,
        status=None,
        outcome=None,
        meta_data=None,
        call_end_time=None,
        expected_status=None,
        call_outcome=None,
    ):
        seen["expected_status"] = expected_status
        return "SELECT 1", []

    async def fake_run(text, values):
        return []

    monkeypatch.setattr(acc, "update_lead_call_completion_details_query", spy_query)
    monkeypatch.setattr(acc, "run_parameterized_query", fake_run)

    await acc.update_lead_call_completion_details(
        id="lead-1",
        status=LeadCallStatus.FINISHED,
        expected_status=LeadCallStatus.PROCESSING,
    )
    assert seen["expected_status"] == LeadCallStatus.PROCESSING


def test_capacity_schemas_reject_a_negative_ceiling():
    """Migration 076 added CHECK (maximum_channels >= 0). Without a schema
    guard a negative value reaches the DB, the accessor swallows the
    CheckViolation into None, and the endpoint returns an opaque 400 instead of
    a 422 naming the field."""
    import pydantic

    from app.schemas.breeze_buddy.core import (
        CreateTelephonyNumberRequest,
        UpdateTelephonyNumberRequest,
    )

    # Passed via **kwargs so the invalid value is a runtime one — a bare
    # literal is rejected statically before the test can exercise pydantic.
    negative = {
        "number": "+15551230000",
        "provider": CallProvider.PLIVO,
        "maximum_channels": -1,
    }
    for model in (CreateTelephonyNumberRequest, UpdateTelephonyNumberRequest):
        with pytest.raises(pydantic.ValidationError):
            model(**negative)

    # Zero is a legitimate "this number carries no calls" setting.
    assert UpdateTelephonyNumberRequest(maximum_channels=0).maximum_channels == 0
