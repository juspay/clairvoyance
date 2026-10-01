"""The console's reports read the replica; the workers never do (30 Sep 2026).

Big Billion Days brings 10x load to a 2-vCPU primary, and the console's
Performance, Versions and Runs reads were ~5% of its time while the replica
sat idle. The replica lags the primary (23 s at the 29 Sep 18:00 peak): a
chart a few seconds old is fine, a walker or a claim acting on a stale row
acts twice. So the split is by caller, and pinned here with fake pools that
record which one served each statement — nothing reaches a database.
"""

from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

import pytest

import app.database as app_database
from app.crm.outreach.db.accessors import enrollment, step, version, workflow
from app.database import READER_TIMEOUT_SECS
from app.database.accessor import (
    get_call_facts_by_runs,
    get_call_stats_by_runs,
    get_leads_by_enrollment_id,
)

T0 = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
RUNS = [("r1", T0, None)]


class FakeConn:
    """Records every statement with the timeout it was given."""

    def __init__(self, error: Optional[Exception] = None) -> None:
        self.error = error
        self.calls: List[Tuple[str, Optional[float]]] = []

    def _record(self, query: str, timeout: Optional[float]) -> None:
        self.calls.append((query, timeout))
        if self.error is not None:
            raise self.error

    async def fetch(self, query: str, *values: Any, timeout=None) -> list:
        self._record(query, timeout)
        return []

    async def fetchrow(self, query: str, *values: Any, timeout=None) -> None:
        self._record(query, timeout)
        return None


class _Acquire:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> FakeConn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakePool:
    def __init__(self, error: Optional[Exception] = None) -> None:
        self.conn = FakeConn(error)
        self.acquire_timeouts: List[Optional[float]] = []

    def acquire(self, timeout: Optional[float] = None) -> _Acquire:
        self.acquire_timeouts.append(timeout)
        return _Acquire(self.conn)


@pytest.fixture
def pools(monkeypatch: pytest.MonkeyPatch) -> Tuple[FakePool, FakePool]:
    writer, reader = FakePool(), FakePool()
    monkeypatch.setattr(app_database, "pool", writer)
    monkeypatch.setattr(app_database, "reader_pool", reader)
    return writer, reader


# Every read behind the console's Performance, Versions and Runs tabs and a
# customer's journey — each is called by the console routes and nothing else.
CONSOLE_READS = [
    ("summary", lambda: enrollment.workflow_summary("m1", "wf", None, None), 4),
    # a windowed summary also counts the window's runs per version (Runs tab)
    ("windowed summary", lambda: enrollment.workflow_summary("m1", "wf", T0, None), 5),
    ("has runs", lambda: enrollment.workflow_has_runs("m1", "wf"), 1),
    ("open runs", lambda: enrollment.workflow_open_runs("m1", "wf"), 2),
    ("report runs", lambda: enrollment.run_endings_in_window("m1", "wf", T0, None), 1),
    ("call stats", lambda: get_call_stats_by_runs("m1", RUNS), 1),
    ("call facts", lambda: get_call_facts_by_runs("m1", RUNS), 1),
    ("versions", lambda: version.list_versions("m1", "wf"), 1),
    # an empty page is counted on its own: two statements
    ("runs page", lambda: enrollment.list_runs("m1", "wf", None, 10, 0), 2),
    ("one run", lambda: enrollment.get_run("m1", "wf", "r1"), 1),
    ("run steps", lambda: step.run_steps("m1", "r1", 200), 1),
    ("run calls", lambda: get_leads_by_enrollment_id("m1", "r1", T0, None), 1),
    ("journey", lambda: enrollment.customer_runs("m1", "c1", 100), 1),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,read,statements", CONSOLE_READS)
async def test_a_console_read_runs_on_the_replica_within_the_minute(
    pools, name, read, statements
) -> None:
    writer, reader = pools
    await read()
    assert writer.conn.calls == [], f"{name} touched the primary"
    assert len(reader.conn.calls) == statements
    # every statement is cancelled at the ceiling, and a black-holed replica
    # cannot hold the connection past it either
    assert {t for _, t in reader.conn.calls} == {READER_TIMEOUT_SECS}
    assert set(reader.acquire_timeouts) == {READER_TIMEOUT_SECS}


@pytest.mark.asyncio
@pytest.mark.parametrize("name,read,statements", CONSOLE_READS)
async def test_a_failing_replica_raises_and_never_retries_on_the_primary(
    monkeypatch: pytest.MonkeyPatch, name, read, statements
) -> None:
    """Retrying a 4-second report on the primary is the load the replica
    exists to take off it: the console shows the error instead."""
    writer, reader = FakePool(), FakePool(error=RuntimeError("replica down"))
    monkeypatch.setattr(app_database, "pool", writer)
    monkeypatch.setattr(app_database, "reader_pool", reader)
    with pytest.raises(RuntimeError, match="replica down"):
        await read()
    assert writer.conn.calls == [], f"{name} retried on the primary"


@pytest.mark.asyncio
@pytest.mark.parametrize("name,read,statements", CONSOLE_READS)
async def test_with_no_replica_the_console_reads_the_primary_still_bounded(
    monkeypatch: pytest.MonkeyPatch, name, read, statements
) -> None:
    """Local runs, or a pod whose replica pool failed to open: the primary
    serves, and since its pool has no ceiling the read carries its own."""
    writer = FakePool()
    monkeypatch.setattr(app_database, "pool", writer)
    monkeypatch.setattr(app_database, "reader_pool", None)
    await read()
    assert len(writer.conn.calls) == statements
    assert {t for _, t in writer.conn.calls} == {READER_TIMEOUT_SECS}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,read",
    [
        ("walker claim", lambda: enrollment.claim_due_runs(100, 60)),
        ("walker's pinned document", lambda: version.get_definition("m1", "wf", 3)),
        ("event worker's live plans", lambda: workflow.live_workflows("m1")),
        ("editor after a save", lambda: workflow.get_workflow("m1", "wf")),
    ],
)
async def test_the_workers_and_the_editor_never_read_the_replica(
    pools, name, read
) -> None:
    """A lagging row here dials twice, advances twice, or shows the editor
    the draft it just replaced."""
    writer, reader = pools
    await read()
    assert reader.conn.calls == [], f"{name} read the replica"
    assert len(writer.conn.calls) == 1
