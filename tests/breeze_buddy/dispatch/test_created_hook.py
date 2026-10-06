"""Workflow-created leads reach the dispatch schedule at creation, not after
the 60 s backlog reconciler. The hook is buddy-side so app/crm stays free of
app.ai imports."""

import asyncio
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import created_hook
from app.ai.voice.agents.breeze_buddy.dispatch.v2.scripts import Rank
from app.database.accessor.breeze_buddy import lead_call_tracker as lct
from app.schemas import ExecutionMode, LeadCallStatus, LeadCallTracker

ROOT = Path(__file__).resolve().parents[3]
WHEN = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)


def _lead(**overrides) -> LeadCallTracker:
    fields = dict(
        id="lead-1",
        reseller_id="r",
        template="t",
        template_id="tmpl-1",
        status=LeadCallStatus.BACKLOG,
        next_attempt_at=WHEN,
        metaData={"workflow_id": "wf-1"},
        execution_mode=ExecutionMode.TELEPHONY,
        payload={},
    )
    fields.update(overrides)
    return LeadCallTracker.model_construct(**fields)


@pytest.fixture
def scheduled(monkeypatch):
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr(created_hook, "schedule_lead", mock)
    # the lead's number is on v2 unless a test says otherwise
    monkeypatch.setattr(created_hook, "_on_v2_number", AsyncMock(return_value=True))
    return mock


def test_entrypoint_registers_the_hook_in_a_fresh_interpreter() -> None:
    # Every CRM_ROLE (the workflow walker included) boots through app.main;
    # a direct import of created_hook here would prove nothing.
    code = (
        "import app.main\n"
        "from app.database.accessor.breeze_buddy import lead_call_tracker as l\n"
        "names = [h.__name__ for h in l._created_hooks]\n"
        "assert '_created_lead_hook' in names, names\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT
    )
    assert result.returncode == 0, result.stderr[-2000:]


async def test_create_runs_through_fire_hooks_and_schedules(scheduled) -> None:
    lct._fire_hooks([created_hook._created_lead_hook], _lead(), "created-lead")
    await asyncio.sleep(0)  # let the spawned task run
    await asyncio.sleep(0)
    scheduled.assert_awaited_once_with(
        "lead-1", WHEN, template_id="tmpl-1", rank=Rank(0, "f", 0)
    )


async def test_workflow_backlog_lead_is_scheduled(scheduled) -> None:
    await created_hook._schedule_created_lead(_lead())
    scheduled.assert_awaited_once_with(
        "lead-1", WHEN, template_id="tmpl-1", rank=Rank(0, "f", 0)
    )


async def test_the_rank_on_the_lead_is_passed_so_its_row_is_not_read(scheduled) -> None:
    meta = {"workflow_id": "wf-1", "priority": {"rank": 2, "order": "n", "event_ms": 5}}
    await created_hook._schedule_created_lead(_lead(metaData=meta))
    scheduled.assert_awaited_once_with(
        "lead-1", WHEN, template_id="tmpl-1", rank=Rank(2, "n", 5)
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": LeadCallStatus.FINISHED},  # capped leads are born FINISHED
        {"metaData": None},
        {"metaData": {}},
        {"next_attempt_at": None},
        {"execution_mode": ExecutionMode.DAILY},
    ],
)
async def test_other_leads_are_not_scheduled(scheduled, overrides) -> None:
    await created_hook._schedule_created_lead(_lead(**overrides))
    scheduled.assert_not_awaited()


def test_hook_never_raises(monkeypatch) -> None:
    def boom(coro, **kwargs):
        coro.close()
        raise RuntimeError("no loop")

    monkeypatch.setattr(created_hook, "spawn_background_task", boom)
    created_hook._created_lead_hook(_lead())  # logs only


async def test_hook_spawns_without_blocking(monkeypatch, scheduled) -> None:
    spawned = []
    monkeypatch.setattr(
        created_hook,
        "spawn_background_task",
        lambda coro, **kw: spawned.append(coro) or coro.close(),
    )
    created_hook._created_lead_hook(_lead())
    assert len(spawned) == 1
    scheduled.assert_not_awaited()


async def test_a_lead_on_a_number_not_on_v2_is_left_to_todays_path(
    scheduled, monkeypatch
) -> None:
    # v2 off, or on for other numbers only: exactly today's behaviour (the
    # backlog reconciler schedules it), nothing pushed at creation.
    monkeypatch.setattr(created_hook, "_on_v2_number", AsyncMock(return_value=False))
    await created_hook._schedule_created_lead(_lead())
    scheduled.assert_not_awaited()


async def test_on_v2_number_reads_nothing_while_v2_is_unused(monkeypatch) -> None:
    from app.ai.voice.agents.breeze_buddy.dispatch.v2 import routes

    monkeypatch.setattr(created_hook, "v2_seen", AsyncMock(return_value=False))
    mode = AsyncMock(return_value=True)
    monkeypatch.setattr(routes, "template_is_v2_accounted", mode)
    assert await created_hook._on_v2_number("tmpl-1") is False
    mode.assert_not_awaited()


@pytest.mark.parametrize(
    "accounted,expected", [(True, True), (False, False), (None, False)]
)
async def test_on_v2_number_follows_the_numbers_mode(
    monkeypatch, accounted, expected
) -> None:
    from app.ai.voice.agents.breeze_buddy.dispatch.v2 import routes

    monkeypatch.setattr(created_hook, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(
        routes, "template_is_v2_accounted", AsyncMock(return_value=accounted)
    )
    assert await created_hook._on_v2_number("tmpl-1") is expected
