"""Lead log lines carry identifiers, never the whole lead (audit X-4).

A finished lead's ``meta_data`` holds the full call transcript. The
lead_call_tracker accessors used to f-string the whole decoded model into
INFO lines ("Lead found: {decoded_result}"), so every lookup or update
serialised a transcript: ~1.18 M renders per 3 h on the dispatcher alone,
lines of 20–88 KB that the log shipper drops anyway.

These tests put a unique marker in the lead's payload and meta_data, run
each changed accessor against a stubbed DB, and assert the log output has
the lead id and the message prefix but not the marker. They also assert
the return value still carries the full lead, so only the log changed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List

import pytest

import app.ai.voice.agents.breeze_buddy.template.hooks as hooks_module
import app.database.accessor.breeze_buddy.lead_call_tracker as acc
from app.ai.voice.agents.breeze_buddy.template.types import HookConfig
from app.core.logger import logger
from app.database.decoder.breeze_buddy.lead_call_tracker import (
    decode_lead_call_tracker,
)
from app.schemas import LeadCallStatus, LeadCallTracker

LEAD_ID = "lead-4f1c-id-under-test"
CALL_ID = "CA-call-id-under-test"
TELEPHONY_NUMBER_ID = "tn-under-test"
MARKER = "TRANSCRIPT-MARKER-9b7e1d"
NEXT_ATTEMPT_AT = datetime(2026, 9, 26, 3, 31, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)


def _row() -> Dict[str, Any]:
    """A lead_call_tracker row as the DB returns it after a finished call."""
    return {
        "id": LEAD_ID,
        "telephony_number_id": TELEPHONY_NUMBER_ID,
        "reseller_id": "reseller-1",
        "template": "order-confirmation",
        "template_id": "6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e",
        "merchant_id": "merchant-1",
        "request_id": "req-1",
        "attempt_count": 1,
        "next_attempt_at": NEXT_ATTEMPT_AT,
        "payload": {"customer_name": f"payload {MARKER}"},
        "meta_data": {
            "transcription": [{"role": "user", "content": MARKER * 50}],
        },
        "recording_url": None,
        "status": "FINISHED",
        "outcome": "CONFIRMED",
        "call_id": CALL_ID,
        "call_initiated_time": NOW,
        "call_end_time": NOW,
        "cost": None,
        "is_locked": False,
        "langfuse_scores": None,
        "execution_mode": "TELEPHONY",
        "call_direction": "OUTBOUND",
        "customer_id": None,
        "enrollment_id": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _lead() -> LeadCallTracker:
    """The decoded lead the accessors return for ``_row()``."""
    lead = decode_lead_call_tracker(_row())  # type: ignore[arg-type]
    assert lead is not None
    return lead


async def _create() -> Any:
    return await acc.create_lead_call_tracker(
        id=LEAD_ID,
        reseller_id="reseller-1",
        template="order-confirmation",
        merchant_id="merchant-1",
        next_attempt_at=NEXT_ATTEMPT_AT,
        payload={"customer_name": f"payload {MARKER}"},
        template_id="6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e",
    )


async def _update_call_details() -> Any:
    return await acc.update_lead_call_details(
        LEAD_ID, LeadCallStatus.PROCESSING, CALL_ID, NOW, TELEPHONY_NUMBER_ID
    )


async def _get_by_call_id() -> Any:
    return await acc.get_lead_by_call_id(CALL_ID)


async def _get_by_id() -> Any:
    return await acc.get_lead_by_id(LEAD_ID)


async def _initiated_time() -> Any:
    return await acc.update_lead_call_initiated_time(CALL_ID, NOW)


async def _initiated_time_by_id() -> Any:
    return await acc.update_lead_call_initiated_time_by_id(LEAD_ID, NOW)


async def _recording_url() -> Any:
    return await acc.update_lead_call_recording_url(CALL_ID, "https://rec/x.mp3")


async def _completion() -> Any:
    return await acc.update_lead_call_completion_details(
        LEAD_ID,
        status=LeadCallStatus.FINISHED,
        outcome="CONFIRMED",
        meta_data={"transcription": MARKER},
        call_end_time=NOW,
    )


async def _template() -> Any:
    return await acc.update_lead_template(
        LEAD_ID, "order-confirmation", "6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e"
    )


# (accessor call, the message prefix that must still lead the line)
CASES: List[tuple[Callable[[], Awaitable[Any]], str]] = [
    (_create, "Lead call tracker created successfully: "),
    (_update_call_details, "Lead updated successfully: "),
    (_get_by_call_id, "Lead found: "),
    (_get_by_id, "Lead found: "),
    (_initiated_time, "Lead updated successfully: "),
    (_initiated_time_by_id, "Lead updated successfully: "),
    (_recording_url, "Lead updated successfully: "),
    (_completion, "Lead call completion details updated successfully: "),
    (_template, "Lead template updated successfully: "),
]


@pytest.fixture
def log_lines():
    lines: List[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG", format="{message}")
    yield lines
    logger.remove(sink)


@pytest.fixture
def stub_db(monkeypatch):
    async def fake_run(query_text, values):
        return [_row()]

    monkeypatch.setattr(acc, "run_parameterized_query", fake_run)
    # Keep the CRM taps out: they are not under test here.
    monkeypatch.setattr(acc, "_created_hooks", [])
    monkeypatch.setattr(acc, "_finished_hooks", [])


@pytest.mark.parametrize("call, prefix", CASES, ids=[c[0].__name__ for c in CASES])
async def test_lead_log_line_has_ids_not_transcript(stub_db, log_lines, call, prefix):
    result = await call()

    # Behaviour unchanged: the caller still gets the whole lead.
    assert result == _lead()
    assert MARKER in str(result.metaData)

    lines = [line for line in log_lines if line.startswith(prefix)]
    assert len(lines) == 1, log_lines
    assert f"id='{LEAD_ID}'" in lines[0]
    assert f"call_id='{CALL_ID}'" in lines[0]
    assert not any(MARKER in line for line in log_lines), log_lines


def test_lead_log_ref_is_a_slice_of_the_old_text():
    """Each field is rendered exactly as the old whole-model text did, so
    log searches like ``telephony_number_id='…'`` and ``next_attempt_at=``
    (dispatcher runbook) still match."""
    lead = _lead()
    ref = acc._lead_log_ref(lead)
    old_text = f"{lead}"
    for name in acc._LEAD_LOG_FIELDS:
        part = f"{name}={getattr(lead, name)!r}"
        assert part in ref, part
        assert part in old_text, part
    assert f"telephony_number_id='{TELEPHONY_NUMBER_ID}'" in ref
    assert "next_attempt_at=datetime.datetime(2026, 9, 26, 3, 31" in ref
    assert MARKER not in ref


def test_lead_log_ref_of_none_matches_old_text():
    assert acc._lead_log_ref(None) == f"{None}"


async def test_outcome_hook_logs_metadata_keys_not_transcript(monkeypatch, log_lines):
    lead = _lead()

    async def fake_update(**kwargs):
        return lead

    monkeypatch.setattr(
        hooks_module, "update_lead_call_completion_details", fake_update
    )
    context = SimpleNamespace(lead=lead, bot=SimpleNamespace())

    await hooks_module.UpdateOutcomeInDatabaseHook().execute(
        context,  # type: ignore[arg-type]
        {"outcome": "CONFIRMED"},
        "confirm_order",
        HookConfig(name="update_outcome_in_database"),
    )

    assert not any(MARKER in line for line in log_lines), log_lines
    updating = [line for line in log_lines if "in database with outcome" in line]
    assert len(updating) == 1, log_lines
    assert updating[0].startswith(f"Updating lead {LEAD_ID} in database with outcome: ")
    assert "metadata keys: ['transcription', 'outcome']" in updating[0]
