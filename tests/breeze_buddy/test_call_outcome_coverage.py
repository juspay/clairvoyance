"""The daily call outcome coverage / consistency report.

``is_consistent`` encodes, per write path, which call outcome columns go with
which legacy word; ``summarize_coverage`` folds the day's grouped rows. The
scheduled task only reads and posts while the columns are being written.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from app.ai.voice.agents.breeze_buddy.managers import (
    call_outcome_coverage as coverage_mod,
)
from app.ai.voice.agents.breeze_buddy.managers.call_outcome_coverage import (
    is_consistent,
    summarize_coverage,
)

SINCE = datetime(2026, 9, 23, tzinfo=timezone.utc)


def row(outcome, status=None, reason=None, end=None, agent=None, rows=1):
    return {
        "outcome": outcome,
        "connection_status": status,
        "connection_reason": reason,
        "end_reason": end,
        "agent_outcome": agent,
        "outcome_source": "LLM" if agent else None,
        "rows": rows,
    }


@pytest.mark.parametrize(
    "r",
    [
        row("PRECHECK_FAILED", "NOT_DIALED", "PRECHECK_FAILED"),
        row("ABORT", "NOT_DIALED", "ABORTED"),
        row("ABORTED", "NOT_DIALED", "ABORTED"),
        row("CALL_LIMIT_REACHED", "NOT_DIALED", "CALL_LIMIT"),
        row("BLOCKED_REDIRECT", "REJECTED", "BLOCKED"),
        row("CAPACITY_REJECTED", "REJECTED", "CAPACITY"),
        row("NO_ANSWER", "BUSY"),
        row("NO_ANSWER", "NO_ANSWER", "TIMEOUT"),
        row("UNKNOWN", "UNKNOWN"),
        row("confirmed", "ANSWERED", end="AGENT_ENDED", agent="CONFIRMED"),
        row("TRANSFERRED", "ANSWERED", end="TRANSFERRED", agent="RESOLVED"),
        row("BUSY", "ANSWERED", end="IDLE_TIMEOUT", agent="CONFIRM"),
        row("BUSY", "ANSWERED", end="CUSTOMER_HANGUP"),
        row("EARLY_HANGUP", "ANSWERED", end="EARLY_HANGUP"),
        row(None, "ANSWERED", end="AGENT_ENDED"),
    ],
)
def test_consistent_rows(r):
    assert is_consistent(r)


@pytest.mark.parametrize(
    "r",
    [
        # a refusal carrying the wrong reason
        row("BLACKLISTED", "NOT_DIALED", "PRECHECK_FAILED"),
        # a carrier status on a row the legacy column says was answered
        row("CONFIRM", "BUSY"),
        # an agent word in legacy with no agent outcome recorded: missed path
        row("CONFIRM", "ANSWERED", end="AGENT_ENDED"),
        # the agent columns disagree with the legacy word
        row("CANCEL", "ANSWERED", end="AGENT_ENDED", agent="CONFIRM"),
        row("UNKNOWN", "ANSWERED", end="PIPELINE_ERROR", agent="CONFIRM"),
    ],
)
def test_inconsistent_rows(r):
    assert not is_consistent(r)


def test_summary_counts_coverage_and_names_the_gaps():
    report = summarize_coverage(
        [
            row("NO_ANSWER", "NO_ANSWER", rows=90),
            row("CONFIRM", "ANSWERED", end="AGENT_ENDED", agent="CONFIRM", rows=5),
            row("CONFIRM", "ANSWERED", end="AGENT_ENDED", rows=3),
            row("PRECHECK_FAILED", None, rows=2),
        ],
        SINCE,
    )
    assert report.finished == 100
    assert report.covered == 98
    assert report.consistent == 95
    assert report.uncovered == [("PRECHECK_FAILED", 2)]
    assert report.mismatches == [("CONFIRM → ANSWERED/-/AGENT_ENDED/-", 3)]
    assert not report.healthy


def test_an_empty_day_is_healthy():
    report = summarize_coverage([], SINCE)
    assert report.coverage == 1.0 and report.consistency == 1.0
    assert report.healthy


async def test_report_does_nothing_while_the_columns_are_off(monkeypatch):
    async def off() -> bool:
        return False

    async def must_not_read(*_a: Any) -> List[Dict[str, Any]]:
        raise AssertionError("read while CALL_OUTCOME_WRITES_ENABLED is off")

    monkeypatch.setattr(coverage_mod, "call_outcome_writes_enabled", off)
    monkeypatch.setattr(coverage_mod, "get_call_outcome_coverage", must_not_read)

    await coverage_mod.report_call_outcome_coverage()


async def test_report_posts_quietly_when_healthy(monkeypatch):
    posts: List[Dict[str, Any]] = []

    async def on() -> bool:
        return True

    async def read(_since: datetime) -> List[Dict[str, Any]]:
        return [row("NO_ANSWER", "NO_ANSWER", rows=10)]

    async def send(**kwargs: Any) -> bool:
        posts.append(kwargs)
        return True

    monkeypatch.setattr(coverage_mod, "call_outcome_writes_enabled", on)
    monkeypatch.setattr(coverage_mod, "get_call_outcome_coverage", read)
    monkeypatch.setattr(coverage_mod.slack_alert, "send", send)

    await coverage_mod.report_call_outcome_coverage()

    (post,) = posts
    assert post["include_tags"] is False
    assert {"name": "Covered", "value": "100.00%"} in post["fields"]
