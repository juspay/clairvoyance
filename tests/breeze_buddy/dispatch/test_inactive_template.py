"""Dialler — an inactive template places no calls.

``is_active=false`` is the merchant's off switch. Intake refuses new leads for
an inactive template; the dialler must also stop the ones queued before it
was switched off (and their retries), so the worker finishes them without
dialling and reports the outcome on the lead's reporting webhook, the way
PRECHECK_FAILED and CALL_LIMIT_REACHED do.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import worker as w
from app.ai.voice.agents.breeze_buddy.dispatch.channel_semaphore import (
    channel_tokens_available,
    init_channel_semaphore,
)
from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
from app.ai.voice.agents.breeze_buddy.managers.pre_checks import PreCheckDecision
from app.schemas import ExecutionMode, LeadCallStatus
from tests.breeze_buddy.dispatch.conftest import make_lead, push_ready

HOOK = "https://m.example/hook"


@pytest.fixture
def webhooks(harness, monkeypatch) -> list:
    """The terminal helper's DB write goes to the harness; its webhook is
    captured here."""
    sent: list = []

    async def _webhook(session, url, data):
        sent.append((url, data))
        return True

    monkeypatch.setattr(
        calls_mod,
        "update_lead_call_completion_details",
        harness.update_lead_call_completion_details,
    )
    monkeypatch.setattr(calls_mod, "send_webhook_with_retry", _webhook)
    return sent


def _use_template(monkeypatch, *, is_active: bool) -> None:
    async def _get(_template_id):
        return SimpleNamespace(id="tmpl-1", is_active=is_active)

    monkeypatch.setattr(w, "get_template_by_id", _get)


def _stop_at_pre_checks(monkeypatch) -> list:
    """Record that the lead got past the inactive-template gate, then stop it
    there (ABORT) so the test never needs the rest of the dial path."""
    reached: list = []

    async def _pre_checks(config, lead, template, session):
        reached.append(lead.id)
        return PreCheckDecision.ABORT, 0

    monkeypatch.setattr(w, "_run_pre_checks_for_lead", _pre_checks)
    return reached


async def _dispatch(harness, fake_redis, lead, worker_uuid: str) -> None:
    harness.add_lead(lead)
    await init_channel_semaphore(harness.number.id, 1)
    await push_ready(fake_redis, lead.id)
    await w.Worker(worker_uuid=worker_uuid)._iteration(session=None)


def _hooked_lead(lead_id: str):
    lead = make_lead(lead_id)
    lead.request_id = "order-1"
    lead.attempt_count = 1
    lead.payload = {**(lead.payload or {}), "reporting_webhook_url": HOOK}
    return lead


async def test_inactive_template_lead_is_finished_and_reported(
    harness, fake_redis, monkeypatch, webhooks
):
    _use_template(monkeypatch, is_active=False)
    reached = _stop_at_pre_checks(monkeypatch)
    lead = _hooked_lead("lead-inactive")
    lead.metaData = {"pre_check_defers": 2}

    await _dispatch(harness, fake_redis, lead, "w-inactive")

    assert harness.call_recorder.calls == []
    assert reached == []
    assert lead.status == LeadCallStatus.FINISHED
    assert lead.outcome == "TEMPLATE_INACTIVE"
    assert await channel_tokens_available(harness.number.id) == 1
    # The lead's existing metaData is kept, not replaced.
    assert harness.completions[-1]["meta_data"] == {
        "pre_check_defers": 2,
        "reason": "Template is inactive",
    }
    assert webhooks == [
        (
            HOOK,
            {
                "outcome": "TEMPLATE_INACTIVE",
                "attemptCount": 2,
                "failureReason": "Template is inactive",
                "orderId": "order-1",
            },
        )
    ]


async def test_no_webhook_without_a_reporting_url(
    harness, fake_redis, monkeypatch, webhooks
):
    _use_template(monkeypatch, is_active=False)
    _stop_at_pre_checks(monkeypatch)
    lead = make_lead("lead-no-hook")

    await _dispatch(harness, fake_redis, lead, "w-no-hook")

    assert lead.outcome == "TEMPLATE_INACTIVE"
    assert webhooks == []


async def test_no_webhook_when_the_finish_write_does_not_land(
    harness, fake_redis, monkeypatch, webhooks
):
    _use_template(monkeypatch, is_active=False)
    _stop_at_pre_checks(monkeypatch)

    async def _write_lost(**_kw):
        return None

    monkeypatch.setattr(calls_mod, "update_lead_call_completion_details", _write_lost)
    lead = _hooked_lead("lead-write-lost")

    await _dispatch(harness, fake_redis, lead, "w-write-lost")

    assert harness.call_recorder.calls == []
    assert webhooks == []


async def test_inactive_template_still_runs_a_test_call(
    harness, fake_redis, monkeypatch, webhooks
):
    _use_template(monkeypatch, is_active=False)
    reached = _stop_at_pre_checks(monkeypatch)
    lead = make_lead("lead-test-call")
    lead.execution_mode = ExecutionMode.TELEPHONY_TEST

    await _dispatch(harness, fake_redis, lead, "w-test-call")

    assert reached == [lead.id]
    assert lead.outcome != "TEMPLATE_INACTIVE"


async def test_inactive_template_still_runs_a_playground_call(
    harness, fake_redis, monkeypatch, webhooks
):
    _use_template(monkeypatch, is_active=False)
    reached = _stop_at_pre_checks(monkeypatch)
    lead = make_lead("lead-playground")
    lead.metaData = {"playground": True}

    await _dispatch(harness, fake_redis, lead, "w-playground")

    assert reached == [lead.id]
    assert lead.outcome != "TEMPLATE_INACTIVE"


async def test_active_template_lead_passes_the_gate(
    harness, fake_redis, monkeypatch, webhooks
):
    _use_template(monkeypatch, is_active=True)
    reached = _stop_at_pre_checks(monkeypatch)
    lead = make_lead("lead-active")

    await _dispatch(harness, fake_redis, lead, "w-active")

    assert reached == [lead.id]
    assert lead.outcome != "TEMPLATE_INACTIVE"
