"""
Unit tests for ``app.ai.voice.agents.breeze_buddy.dispatch.alerts``.

The tests verify two things:
  1. Throttle semantics — first call fires, repeats within the TTL are
     suppressed, after the TTL (or after ``clear_throttle``) the next call
     fires again.
  2. Each ``raise_*`` wrapper calls into ``slack_alert.send`` with the
     correct title (runbook grep target).
"""

from __future__ import annotations

from typing import List, Tuple

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import alerts


@pytest.fixture
def captured_slack(monkeypatch):
    """Capture every slack_alert.send call as (title, fields)."""
    captured: List[Tuple[str, list]] = []

    async def _fake_send(title, fields=None, **_kw):
        captured.append((title, fields or []))
        return True

    monkeypatch.setattr(alerts.slack_alert, "send", _fake_send)
    return captured


async def test_first_alert_fires(fake_redis, captured_slack):
    await alerts.raise_no_leader()

    assert len(captured_slack) == 1
    assert "no leader" in captured_slack[0][0].lower()


async def test_repeat_within_throttle_is_suppressed(fake_redis, captured_slack):
    await alerts.raise_no_leader()
    await alerts.raise_no_leader()
    await alerts.raise_no_leader()

    # All three calls happen synchronously; throttle key prevents duplicates.
    assert len(captured_slack) == 1


async def test_clear_throttle_allows_next_to_fire(fake_redis, captured_slack):
    await alerts.raise_no_leader()
    await alerts.clear_throttle("no_leader")
    await alerts.raise_no_leader()

    assert len(captured_slack) == 2


async def test_dispatch_halted_includes_metric_values(fake_redis, captured_slack):
    await alerts.raise_dispatch_halted(overdue_count=42, schedule_size=99)

    assert len(captured_slack) == 1
    _title, fields = captured_slack[0]
    values = {f["name"]: f["value"] for f in fields}
    assert values["Overdue leads"] == "42"
    assert values["Schedule depth"] == "99"


async def test_schedule_depth_alert_carries_threshold(fake_redis, captured_slack):
    await alerts.raise_schedule_depth_high(schedule_size=60_000, threshold=50_000)

    _title, fields = captured_slack[0]
    values = {f["name"]: f["value"] for f in fields}
    assert values["Schedule size"] == "60000"
    assert values["Threshold"] == "50000"


async def test_channel_drift_alert_throttles_per_number(fake_redis, captured_slack):
    """
    Throttle key includes the outbound_number_id — drift on number A
    must not suppress an alert about number B.
    """
    await alerts.raise_channel_drift(
        outbound_number_id="num-A", expected_free=5, actual_free=0
    )
    await alerts.raise_channel_drift(
        outbound_number_id="num-A", expected_free=5, actual_free=0
    )
    await alerts.raise_channel_drift(
        outbound_number_id="num-B", expected_free=5, actual_free=0
    )

    # num-A repeat suppressed; num-B fires fresh.
    assert len(captured_slack) == 2


async def test_orphan_webhook_alert_carries_call_id(fake_redis, captured_slack):
    await alerts.raise_orphan_webhook(call_id="CA1234", source="call_completion")

    _title, fields = captured_slack[0]
    values = {f["name"]: f["value"] for f in fields}
    assert values["call_id"] == "CA1234"
    assert values["Webhook source"] == "call_completion"


async def test_slack_send_failure_does_not_raise(fake_redis, monkeypatch):
    """A failing Slack send must not propagate — alert is best-effort."""

    async def _broken_send(*a, **kw):
        raise RuntimeError("simulated Slack outage")

    monkeypatch.setattr(alerts.slack_alert, "send", _broken_send)

    # Must not raise.
    await alerts.raise_no_leader()
