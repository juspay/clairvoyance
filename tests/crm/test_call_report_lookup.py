"""record.call_report: a finished call's call.completed, read back by the
key it was stored under, as its consumers are handed it — so the walker can
hear a report the consumer could not deliver (the run stood on the call
square, which hears nothing)."""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

import app.crm.record.events as events
from app.ai.voice.agents.breeze_buddy.crm_mirror import SOURCE_TELEPHONY, _event_key
from app.crm.record.db.queries import event_by_key_query
from app.crm.record.extractors import CALL_REPORT_SOURCES
from app.crm.record.schemas import RawEvent

NOW = datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)


def test_the_key_is_the_one_the_mirror_stores_the_report_under() -> None:
    """Pinned against crm_mirror: a drift here is a lookup that never finds
    the report, and the run waits 1440 min again."""
    assert events.call_report_key("sid-1") == _event_key("call.completed", "sid-1")
    assert events.CALL_REPORT_SOURCE == SOURCE_TELEPHONY
    assert events.CALL_REPORT_SOURCE in CALL_REPORT_SOURCES


def test_the_read_is_the_dedupe_key_and_nothing_else() -> None:
    sql, params = event_by_key_query("m1", "telephony", "call.completed:sid-1")
    assert "WHERE merchant_id = $1 AND source = $2 AND external_id = $3" in sql
    assert params == ["m1", "telephony", "call.completed:sid-1"]


def _event() -> RawEvent:
    return RawEvent(
        id="ev-1",
        merchant_id="m1",
        source="telephony",
        topic="call.completed",
        schema_version="1",
        external_id="call.completed:sid-1",
        payload={"lead_id": "L", "outcome": "BUSY"},
        received_at=NOW,
        occurred_at=NOW,
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    stored: Optional[RawEvent],
    variables: Optional[Dict[str, Any]],
    asked: List[Any],
) -> None:
    async def event_by_key(merchant_id: str, source: str, key: str) -> Any:
        asked.append((merchant_id, source, key))
        return stored

    async def variables_for(event: RawEvent) -> Any:
        return variables

    monkeypatch.setattr(events.accessor, "event_by_key", event_by_key)
    monkeypatch.setattr(events, "variables_for", variables_for)


def test_a_recorded_report_comes_back_with_its_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: List[Any] = []
    _install(monkeypatch, _event(), {"summary": "x"}, asked)
    found = asyncio.run(events.call_report("m1", "sid-1"))
    assert found is not None
    event, variables = found
    assert asked == [("m1", "telephony", "call.completed:sid-1")]
    assert (event.id, variables) == ("ev-1", {"summary": "x"})


def test_a_report_not_yet_recorded_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, None, {}, [])
    assert asyncio.run(events.call_report("m1", "sid-1")) is None


def test_a_report_no_consumer_could_decode_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consumer would have quarantined it, so nobody heard it: the walker
    must not hear it either — it arms the wait as before."""
    _install(monkeypatch, _event(), None, [])
    assert asyncio.run(events.call_report("m1", "sid-1")) is None
