"""Calls to other systems a worker pass defers until after its commit."""

import asyncio
from typing import List

import pytest

from app.crm.record import workers
from app.crm.shared import after_commit


def test_outside_a_pass_nothing_is_deferred() -> None:
    async def job() -> None:
        return None

    assert after_commit.defer(job) is False


def test_inside_a_pass_the_job_waits_for_run_and_one_failure_stops_nothing() -> None:
    done: List[str] = []

    async def broken() -> None:
        raise RuntimeError("redis down")

    async def ok() -> None:
        done.append("ok")

    with after_commit.collecting() as later:
        assert after_commit.defer(broken) is True
        assert after_commit.defer(ok) is True
    assert done == []  # deferred, not run
    asyncio.run(after_commit.run(later))
    assert done == ["ok"]
    assert after_commit.defer(ok) is False  # the pass is over


def test_the_event_worker_runs_deferred_calls_after_its_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: List[str] = []

    async def queue_call() -> None:
        order.append("queue")

    async def fake_atomically(fn, limit, discovered):  # the pass's transaction
        after_commit.defer(queue_call)
        order.append("commit")
        return []

    monkeypatch.setattr(workers, "atomically", fake_atomically)
    asyncio.run(workers.run_pass(10))
    assert order == ["commit", "queue"]
