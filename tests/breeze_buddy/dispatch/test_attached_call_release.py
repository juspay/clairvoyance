"""A call placed for a lead the merchant finished mid-dial gives its line back
exactly once.

The dispatcher stamps such a call on the finished lead
(``CALL_ATTACHED_AFTER_FINISH``) and keeps its channel while it lives. The
answer path hangs it up without an agent, so the completion handler never runs
for it on Exotel / Vobiz / Plivo: the provider's ``completed`` status (or an
unanswered status) is its only end-of-call signal. Every end-of-call path
claims the one release; duplicates and the other paths release nothing.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
from app.database.queries.breeze_buddy.dispatch import (
    count_processing_by_telephony_number_query,
)
from app.database.queries.breeze_buddy.lead_call_tracker import (
    claim_attached_call_release_query,
)
from app.schemas import CallDirection, ExecutionMode, LeadCallStatus
from app.schemas.breeze_buddy.core import CALL_ATTACHED_AFTER_FINISH, LeadCallTracker

pytestmark = pytest.mark.asyncio

CALL_ID = "CA-attached"


def _attached_lead(marker: Dict[str, Any]) -> LeadCallTracker:
    return LeadCallTracker(
        id="lead-aborted",
        reseller_id="res-1",
        template="welcome",
        template_id="tmpl-1",
        merchant_id="merchant-1",
        attempt_count=1,
        payload={"customer_mobile_number": "+15551234567"},
        status=LeadCallStatus.FINISHED,
        outcome="ABORT",
        execution_mode=ExecutionMode.TELEPHONY,
        call_direction=CallDirection.OUTBOUND,
        call_id=CALL_ID,
        telephony_number_id="num-1",
        metaData={CALL_ATTACHED_AFTER_FINISH: marker},
    )


class _Row:
    """The lead row as the DB holds it; each lookup returns a fresh snapshot,
    like ``get_lead_by_call_id`` does."""

    def __init__(self) -> None:
        self.marker: Dict[str, Any] = {"at": "now", "call_id": CALL_ID}
        self.completion_updates: List[Dict[str, Any]] = []
        self.released: List[str] = []

    def snapshot(self) -> LeadCallTracker:
        return _attached_lead(dict(self.marker))


@pytest.fixture
def row(monkeypatch: pytest.MonkeyPatch, fake_redis) -> Any:
    r = _Row()

    async def _lookup(call_id: str):
        return r.snapshot() if call_id == CALL_ID else None

    async def _claim(lead_id: str) -> bool:  # the query's rule, see the PG test below
        if "released_at" in r.marker:
            return False
        r.marker["released_at"] = "now"
        return True

    async def _release(lead: LeadCallTracker) -> None:
        r.released.append(lead.id)

    async def _noop(*a: Any, **k: Any) -> None:
        return None

    async def _complete(**kwargs: Any) -> None:
        r.completion_updates.append(kwargs)

    monkeypatch.setattr(calls_mod, "get_lead_by_call_id", _lookup)
    monkeypatch.setattr(calls_mod, "claim_attached_call_release", _claim)
    monkeypatch.setattr(calls_mod, "_release_call_resources", _release)
    monkeypatch.setattr(calls_mod, "safe_release_pod", _noop)
    monkeypatch.setattr(calls_mod, "update_lead_call_completion_details", _complete)
    monkeypatch.setattr(calls_mod, "COMPLETED_RECONCILE_DELAY_SECONDS", 0)
    return r


async def test_an_answered_call_on_an_aborted_lead_gives_its_line_back_on_completed(
    row: Any,
) -> None:
    # answered, hung up at answer (no agent), provider sends `completed`
    await calls_mod.reconcile_completed_call(CALL_ID)

    assert row.released == ["lead-aborted"]
    assert row.completion_updates == []  # the merchant's ABORT stays


async def test_every_end_of_call_path_and_duplicate_releases_the_line_once(
    row: Any,
) -> None:
    await calls_mod.reconcile_completed_call(CALL_ID)
    await calls_mod.reconcile_completed_call(CALL_ID)  # duplicate webhook
    await calls_mod.handle_unanswered_calls(CALL_ID)
    await calls_mod.handle_call_completion(
        CALL_ID, outcome="ANSWERED"
    )  # Twilio: agent ran

    assert row.released == ["lead-aborted"]
    assert row.completion_updates == []


async def test_an_unanswered_call_on_an_aborted_lead_releases_once(row: Any) -> None:
    await calls_mod.handle_unanswered_calls(CALL_ID)
    await calls_mod.handle_unanswered_calls(CALL_ID)

    assert row.released == ["lead-aborted"]
    assert row.completion_updates == []


async def test_a_completion_for_an_aborted_lead_never_overwrites_its_outcome(
    row: Any,
) -> None:
    lead = await calls_mod.handle_call_completion(CALL_ID, outcome="ANSWERED")

    assert lead is not None and lead.outcome == "ABORT"
    assert row.released == ["lead-aborted"]
    assert row.completion_updates == []


# ---------------------------------------------------------------------------
# The SQL itself, on a real Postgres (DSN-gated, like test_inbound_capacity_gate)
# ---------------------------------------------------------------------------


async def test_the_release_claim_and_the_token_count_on_postgres() -> None:
    """Runs the REAL claim and count queries. Skipped unless CAPGATE_TEST_DSN
    points at a throwaway Postgres (see test_inbound_capacity_gate)."""
    dsn = os.environ.get("CAPGATE_TEST_DSN")
    if not dsn:
        pytest.skip("set CAPGATE_TEST_DSN to run the attached-call SQL on Postgres")

    import asyncpg

    conn = await asyncpg.connect(dsn)
    now = datetime.now(timezone.utc)
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
                execution_mode VARCHAR(50), meta_data JSONB,
                call_initiated_time TIMESTAMPTZ, updated_at TIMESTAMPTZ)""")
        await conn.execute(
            "INSERT INTO telephony_numbers (id,number,provider,status) "
            "VALUES ('n-1','+15550001','EXOTEL','AVAILABLE')"
        )
        marker = {CALL_ATTACHED_AFTER_FINISH: {"at": "x", "call_id": CALL_ID}}
        released = {
            CALL_ATTACHED_AFTER_FINISH: {"at": "x", "call_id": "c", "released_at": "x"}
        }
        rows = [
            # id, status, meta, dialled minutes ago -> counted as holding a line?
            ("live", "FINISHED", marker, 2, True),
            ("released", "FINISHED", released, 2, False),
            ("too-old", "FINISHED", marker, 300, False),
            ("plain-finished", "FINISHED", {"x": 1}, 2, False),
            ("processing", "PROCESSING", {}, 2, True),
        ]
        import json

        for lid, status, meta, ago, _ in rows:
            await conn.execute(
                "INSERT INTO lead_call_tracker VALUES "
                "($1,'n-1',$2,'OUTBOUND','TELEPHONY',$3::jsonb,$4,NOW())",
                lid,
                status,
                json.dumps(meta),
                now - timedelta(minutes=ago),
            )

        text, values = count_processing_by_telephony_number_query(240)
        counted = {
            r["telephony_number_id"]: r["in_flight"]
            for r in await conn.fetch(text, *values)
        }
        assert counted == {"n-1": sum(1 for *_, held in rows if held)}

        # the claim: exactly one winner, the marker stays, a released row never wins
        text, values = claim_attached_call_release_query("live")
        first = await conn.fetch(text, *values)
        second = await conn.fetch(text, *values)
        assert [r["id"] for r in first] == ["live"] and second == []
        meta = json.loads(
            await conn.fetchval(
                "SELECT meta_data FROM lead_call_tracker WHERE id='live'"
            )
        )
        assert meta[CALL_ATTACHED_AFTER_FINISH]["call_id"] == CALL_ID
        assert "released_at" in meta[CALL_ATTACHED_AFTER_FINISH]
        for lid in ("released", "plain-finished", "processing"):
            text, values = claim_attached_call_release_query(lid)
            assert await conn.fetch(text, *values) == []

        # once released, the line no longer counts as held
        text, values = count_processing_by_telephony_number_query(240)
        counted = {
            r["telephony_number_id"]: r["in_flight"]
            for r in await conn.fetch(text, *values)
        }
        assert counted == {"n-1": 1}  # only the PROCESSING lead
    finally:
        await conn.execute("DROP TABLE IF EXISTS lead_call_tracker, telephony_numbers")
        await conn.close()


async def test_v2_seed_ledger_and_states_hold_the_same_lines_as_the_count() -> None:
    """The v2 seed and ledger read the same calls as holding a line as the channel
    count does: a call attached to a lead finished mid-dial holds one until its end
    releases it (``released_at``). Skipped unless CAPGATE_TEST_DSN is set."""
    dsn = os.environ.get("CAPGATE_TEST_DSN")
    if not dsn:
        pytest.skip("set CAPGATE_TEST_DSN to run the attached-call SQL on Postgres")

    import json

    import asyncpg

    from app.database.queries.breeze_buddy.dispatch import (
        get_lead_dispatch_states_query,
        get_live_calls_on_number_query,
        get_live_calls_on_numbers_query,
    )

    conn = await asyncpg.connect(dsn)
    now = datetime.now(timezone.utc)
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
                execution_mode VARCHAR(50), meta_data JSONB, call_id VARCHAR(255),
                is_locked BOOLEAN DEFAULT FALSE, template_id UUID,
                next_attempt_at TIMESTAMPTZ, call_initiated_time TIMESTAMPTZ,
                updated_at TIMESTAMPTZ)""")
        await conn.execute(
            "INSERT INTO telephony_numbers (id,number,provider,status) "
            "VALUES ('n-1','+15550001','PLIVO','AVAILABLE')"
        )
        attached = {
            CALL_ATTACHED_AFTER_FINISH: {"at": now.isoformat(), "call_id": "c1"}
        }
        released = {
            CALL_ATTACHED_AFTER_FINISH: {
                "at": now.isoformat(),
                "call_id": "c2",
                "released_at": now.isoformat(),
            }
        }
        rows = [
            # id, status, meta, dialled minutes ago -> holds a line?
            ("live", "FINISHED", attached, 2, True),
            ("released", "FINISHED", released, 2, False),
            ("too-old", "FINISHED", attached, 300, False),
            ("plain-finished", "FINISHED", {"x": 1}, 2, False),
            ("processing", "PROCESSING", {}, 2, True),
        ]
        for lid, status, meta, ago, _ in rows:
            await conn.execute(
                "INSERT INTO lead_call_tracker (id, telephony_number_id, status, "
                "call_direction, execution_mode, meta_data, call_id, "
                "call_initiated_time, updated_at) "
                "VALUES ($1,'n-1',$2,'OUTBOUND','TELEPHONY',$3::jsonb,$1,$4,NOW())",
                lid,
                status,
                json.dumps(meta),
                now - timedelta(minutes=ago),
            )
        holding = {lid for lid, *_, held in rows if held}

        text, values = get_live_calls_on_number_query("n-1", 240)
        assert {r["id"] for r in await conn.fetch(text, *values)} == holding
        text, values = get_live_calls_on_numbers_query(["n-1"], 240)
        assert {r["id"] for r in await conn.fetch(text, *values)} == holding

        text, values = get_lead_dispatch_states_query(["live", "released"])
        attached_at = {
            r["id"]: r["call_attached_at"] for r in await conn.fetch(text, *values)
        }
        assert attached_at["live"] is not None  # still owns its line
        assert attached_at["released"] is None  # its end released the line
    finally:
        await conn.execute("DROP TABLE IF EXISTS lead_call_tracker, telephony_numbers")
        await conn.close()
