"""The outcome check: before a finished telephony call's call.completed is
sent, the built-in outcome_correctness eval gets at most two seconds to say
which of the agent's own outcome words the call should have ended with; a
confident, different answer becomes the lead's outcome and the event's."""

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, cast
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from app.ai.voice.agents.breeze_buddy import crm_mirror
from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.preset import (
    outcome_eval,
)
from app.schemas import CallDirection, ExecutionMode, LeadCallStatus, LeadCallTracker
from app.services.evals.engines.common import ChoiceResult, Verdict
from app.services.evals.preset.outcome_correctness import (
    NO_DECISION,
    OUTCOME_CORRECTNESS,
)

TEMPLATE_ID = "00000000-0000-0000-0000-000000000001"

#: The global row migration 083 seeds.
BUILTIN: Dict[str, Any] = {
    "id": "builtin-id",
    "template_id": None,
    "evaluation_type": "CONVERSATION_EVALS",
    "name": OUTCOME_CORRECTNESS,
    "enabled": True,
    "topics": [],
    "configuration": {
        "engine": "structured",
        "provider": "typesafe",
        "model": "jev-1.13.0",
        "questions": [],
        "min_confidence": 0.8,
    },
}


def _template() -> Any:
    """An agent with two outcome words."""

    def function(name: str, word: str) -> Dict[str, Any]:
        hook = {
            "name": "update_outcome_in_database",
            "expected_fields": {"outcome": {"source": "static", "value": word}},
        }
        return {"name": name, "description": f"customer {name}s", "hooks": [hook]}

    flow = {
        "nodes": [
            {
                "functions": [
                    function("confirm", "CONFIRM"),
                    function("cancel", "CANCEL"),
                ]
            }
        ]
    }
    return SimpleNamespace(flow=flow, configurations=None)


def _verdict(value: Optional[str], confidence: Optional[float]) -> Verdict:
    return Verdict(
        engine="structured",
        provider="typesafe",
        model="jev-1.13.0",
        result=[
            ChoiceResult(
                key="outcome", label="Outcome", value=value, confidence=confidence
            )
        ],
    )


def make_lead(**overrides: Any) -> LeadCallTracker:
    values: Dict[str, Any] = dict(
        id="lead-1",
        reseller_id="breeze",
        template="t",
        template_id=TEMPLATE_ID,
        merchant_id="shop",
        payload={"customer_mobile_number": "+919999999999"},
        metaData={},
        status=LeadCallStatus.FINISHED,
        outcome="CANCEL",
        call_id="CA-1",
        attempt_count=0,
        call_initiated_time=datetime(2026, 10, 7, 10, tzinfo=timezone.utc),
        call_end_time=datetime(2026, 10, 7, 10, 5, tzinfo=timezone.utc),
        execution_mode=ExecutionMode.TELEPHONY,
        call_direction=CallDirection.OUTBOUND,
    )
    values.update(overrides)
    return LeadCallTracker(**values)


def _check(
    monkeypatch,
    lead: LeadCallTracker,
    verdict: Any = None,
    builtin: Any = "default",
    template: Any = "default",
    context: Any = "default",
) -> SimpleNamespace:
    async def update(
        id: str, outcome: str, agent_outcome: Optional[str]
    ) -> LeadCallTracker:
        return lead.model_copy(update={"outcome": outcome})

    mocks = SimpleNamespace(
        builtin=AsyncMock(return_value=BUILTIN if builtin == "default" else builtin),
        template=AsyncMock(
            return_value=_template() if template == "default" else template
        ),
        context=AsyncMock(
            return_value=(
                {
                    "source_id": "lead-1",
                    "reseller_id": "breeze",
                    "merchant_id": "shop",
                    "template_id": TEMPLATE_ID,
                    "started_at": datetime(2026, 10, 7, 10, tzinfo=timezone.utc),
                    "transcript": [{"role": "user", "content": "yes"}],
                }
                if context == "default"
                else context
            )
        ),
        evals=AsyncMock(
            side_effect=verdict if isinstance(verdict, Exception) else None,
            return_value=verdict,
        ),
        update=AsyncMock(side_effect=update),
        failure=AsyncMock(),
    )
    monkeypatch.setattr(outcome_eval, "get_outcome_correctness", mocks.builtin)
    monkeypatch.setattr(outcome_eval, "get_template_by_id", mocks.template)
    monkeypatch.setattr(outcome_eval, "get_analysis_context", mocks.context)
    monkeypatch.setattr(outcome_eval, "analyze_evals", mocks.evals)
    monkeypatch.setattr(outcome_eval, "set_eval_outcome", mocks.update)
    monkeypatch.setattr(outcome_eval, "save_evaluation_failure", mocks.failure)
    return mocks


# ---------------------------------------------------------------------------
# the check: ask, compare, save
# ---------------------------------------------------------------------------


async def test_the_builtin_eval_is_asked_about_the_agents_own_words(monkeypatch):
    lead = make_lead(outcome="BUSY")
    mocks = _check(monkeypatch, lead, _verdict("CONFIRM", 0.9))

    assert await outcome_eval.checked_outcome(lead) == "CONFIRM"

    mocks.builtin.assert_awaited_once_with(TEMPLATE_ID)
    judged, evaluation, channel = mocks.evals.await_args.args
    # the judge sees the transcript and the agent's word
    assert judged["recorded_outcome"] == "BUSY" and judged["channel"] == "VOICE"
    assert judged["transcript"] == [{"role": "user", "content": "yes"}]
    # stored against the built-in row, under its name
    assert (evaluation["id"], evaluation["name"]) == ("builtin-id", OUTCOME_CORRECTNESS)
    configuration = evaluation["configuration"]
    assert configuration["model"] == "jev-1.13.0"
    (question,) = configuration["questions"]
    assert list(question["criteria"]) == ["CONFIRM", "CANCEL", NO_DECISION]
    assert question["criteria"]["CONFIRM"] == "customer confirms"
    # the eval's word, with the agent's word it replaced
    mocks.update.assert_awaited_once_with("lead-1", "CONFIRM", "BUSY")


@pytest.mark.parametrize(
    "value, confidence",
    [
        ("CONFIRM", 0.79),  # below the row's 0.8
        (NO_DECISION, 0.99),  # none of the agent's words
        ("CANCEL", 0.99),  # the agent's own word
    ],
)
async def test_the_agents_word_stands(monkeypatch, value, confidence):
    lead = make_lead()
    mocks = _check(monkeypatch, lead, _verdict(value, confidence))

    assert await outcome_eval.checked_outcome(lead) == "CANCEL"

    mocks.update.assert_not_awaited()


@pytest.mark.parametrize(
    "lead_overrides, builtin, template",
    [
        ({"outcome": "TRANSFERRED"}, "default", "default"),  # how the call went
        ({"execution_mode": ExecutionMode.DAILY}, "default", "default"),  # web call
        ({"template_id": None}, "default", "default"),
        ({}, None, "default"),  # the agent turned it off
        ({}, "default", None),  # the template is gone
        # the LLM writes the outcome freely: no list of words to choose from
        (
            {},
            "default",
            SimpleNamespace(
                flow={
                    "functions": [
                        {
                            "name": "set",
                            "description": "d",
                            "hooks": [
                                {
                                    "name": "update_outcome_in_database",
                                    "expected_fields": {"outcome": {"source": "llm"}},
                                }
                            ],
                        }
                    ]
                },
                configurations=None,
            ),
        ),
        ({}, "default", SimpleNamespace(flow={"nodes": []}, configurations=None)),
    ],
)
async def test_calls_not_checked_keep_the_agents_word(
    monkeypatch, lead_overrides, builtin, template
):
    lead = make_lead(**lead_overrides)
    mocks = _check(monkeypatch, lead, _verdict("CONFIRM", 0.99), builtin, template)

    assert await outcome_eval.checked_outcome(lead) == lead.outcome

    mocks.evals.assert_not_awaited()
    mocks.update.assert_not_awaited()


async def test_a_call_with_nothing_to_judge_keeps_the_agents_word(monkeypatch):
    lead = make_lead()
    mocks = _check(monkeypatch, lead, _verdict("CONFIRM", 0.99), context=None)

    assert await outcome_eval.checked_outcome(lead) == "CANCEL"

    mocks.evals.assert_not_awaited()


async def test_a_failing_eval_keeps_the_agents_word(monkeypatch):
    lead = make_lead()
    _check(monkeypatch, lead, RuntimeError("engine down"))

    assert await outcome_eval.checked_outcome(lead) == "CANCEL"


async def test_an_unsaved_answer_is_not_sent(monkeypatch):
    lead = make_lead()
    mocks = _check(monkeypatch, lead, _verdict("CONFIRM", 0.95))
    mocks.update.side_effect = None
    mocks.update.return_value = None  # the write failed, or the lead moved on

    # lead and CRM agree
    assert await outcome_eval.checked_outcome(lead) == "CANCEL"


async def test_an_eval_past_two_seconds_keeps_the_agents_word(monkeypatch):
    async def hangs(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(5)  # the eval provider does not answer

    lead = make_lead()
    mocks = _check(monkeypatch, lead, _verdict("CONFIRM", 0.99))
    mocks.evals.side_effect = hangs
    assert outcome_eval._MAX_WAIT_SECONDS == 2
    monkeypatch.setattr(outcome_eval, "_MAX_WAIT_SECONDS", 0.05)
    started = time.monotonic()

    assert await outcome_eval.checked_outcome(lead) == "CANCEL"

    assert time.monotonic() - started < 1
    mocks.update.assert_not_awaited()
    # counted as a timeout in outcome analytics: a FAILED row, saved in the
    # background
    await asyncio.sleep(0)
    mocks.failure.assert_awaited_once()
    args = mocks.failure.await_args.args
    assert args[:3] == ("builtin-id", "CONVERSATION_EVALS", "lead-1")
    assert args[-1].startswith(outcome_eval.TIMED_OUT)


async def test_an_eval_that_failed_leaves_a_failed_row(monkeypatch):
    lead = make_lead()
    mocks = _check(monkeypatch, lead, None)  # analyze_evals logged and stored nothing

    assert await outcome_eval.checked_outcome(lead) == "CANCEL"

    await asyncio.sleep(0)
    mocks.failure.assert_awaited_once()
    assert mocks.failure.await_args.args[-1] == "EVAL_FAILED"


async def test_an_answered_check_leaves_no_failed_row(monkeypatch):
    lead = make_lead()
    mocks = _check(monkeypatch, lead, _verdict("CANCEL", 0.99))

    assert await outcome_eval.checked_outcome(lead) == "CANCEL"

    await asyncio.sleep(0)
    mocks.failure.assert_not_awaited()


async def test_a_failed_row_that_cannot_be_built_never_breaks_the_check(
    monkeypatch,
):
    lead = make_lead()
    # a context without the ids a FAILED row needs
    mocks = _check(
        monkeypatch,
        lead,
        None,
        context={
            "source_id": "lead-1",
            "transcript": [{"role": "user", "content": "y"}],
        },
    )

    assert await outcome_eval.checked_outcome(lead) == "CANCEL"
    await asyncio.sleep(0)
    mocks.failure.assert_not_awaited()


# ---------------------------------------------------------------------------
# the mismatch log
# ---------------------------------------------------------------------------


@pytest.fixture
def mismatches():
    """The extra fields of each outcome-mismatch line logged."""
    records: List[Dict[str, Any]] = []
    sink = logger.add(
        # the logger's own keys (``_log_context``) aside
        lambda message: records.append(
            {
                key: value
                for key, value in message.record["extra"].items()
                if not key.startswith("_")
            }
        ),
        level="INFO",
        filter=lambda record: record["message"].startswith("Outcome mismatch"),
    )
    yield records
    logger.remove(sink)


@pytest.mark.parametrize(
    "value, confidence, saved, result",
    [
        ("CONFIRM", 0.9, True, "replaced"),
        ("CONFIRM", 0.5, True, "kept: not confident enough"),
        (NO_DECISION, 0.99, True, "kept: no decision"),
        ("CONFIRM", 0.9, False, "kept: not saved"),
    ],
)
async def test_a_different_eval_outcome_is_logged(
    monkeypatch, mismatches, value, confidence, saved, result
):
    lead = make_lead(outcome="CANCEL")
    mocks = _check(monkeypatch, lead, _verdict(value, confidence))
    if not saved:
        mocks.update.side_effect = None
        mocks.update.return_value = None

    await outcome_eval.checked_outcome(lead)

    # the call's ids and the two outcomes, nothing more
    assert mismatches == [
        {
            "component": outcome_eval.LOG_COMPONENT,
            "lead_id": "lead-1",
            "call_sid": "CA-1",
            "agent_outcome": "CANCEL",
            "eval_outcome": value,
            "eval_confidence": confidence,
            "min_confidence": 0.8,
            "result": result,
        }
    ]


async def test_a_mismatch_names_the_run_that_placed_the_call(monkeypatch, mismatches):
    lead = make_lead(outcome="CANCEL", enrollment_id="run-1")
    _check(monkeypatch, lead, _verdict("CONFIRM", 0.9))

    await outcome_eval.checked_outcome(lead)

    (line,) = mismatches
    assert line["enrollment_id"] == "run-1"


async def test_an_agreeing_eval_logs_no_mismatch(monkeypatch, mismatches):
    lead = make_lead(outcome="cancel")
    _check(monkeypatch, lead, _verdict("CANCEL", 0.99))

    await outcome_eval.checked_outcome(lead)

    assert mismatches == []


# ---------------------------------------------------------------------------
# the call.completed tap
# ---------------------------------------------------------------------------


async def test_call_completed_carries_the_checked_outcome(monkeypatch):
    spawned: List[Any] = []
    mirror = AsyncMock()
    monkeypatch.setattr(
        crm_mirror,
        "spawn_background_task",
        lambda coro, name="": spawned.append(asyncio.ensure_future(coro)),
    )
    monkeypatch.setattr(crm_mirror, "mirror_to_crm", mirror)
    monkeypatch.setattr(crm_mirror, "call_facts", AsyncMock(return_value={}))
    monkeypatch.setattr(
        crm_mirror, "checked_outcome", AsyncMock(return_value="CONFIRM")
    )

    crm_mirror._finished_lead_tap(make_lead(outcome="CANCEL"))
    await asyncio.gather(*spawned)

    assert mirror.await_args is not None
    assert mirror.await_args.args[0] == "call.completed"
    assert mirror.await_args.kwargs["outcome"] == "CONFIRM"


# ---------------------------------------------------------------------------
# the write: the outcome and its record in one statement
# ---------------------------------------------------------------------------


def test_the_eval_outcome_is_written_with_the_agents_word():
    from app.database.queries.breeze_buddy.lead_call_tracker import (
        set_eval_outcome_query,
    )

    query, values = set_eval_outcome_query("lead-1", "CONFIRM", "BUSY")

    assert values == ["lead-1", "CONFIRM", "BUSY", "FINISHED"]
    assert '"outcome" = $2' in query
    # the agent's word stays in its own column (migration 084): every other
    # outcome write already set it; a lead a build from before the column
    # wrote gets the word the check read
    assert '"agent_outcome" = COALESCE("agent_outcome", $3)' in query
    assert "meta_data" not in query
    # compare-and-set: only a FINISHED lead whose outcome is still the word
    # the check read, so an outcome written since is never overwritten
    assert '"status" = $4' in query
    assert '"outcome" IS NOT DISTINCT FROM $3' in query


def test_every_other_outcome_write_sets_the_agents_word():
    from app.database.queries.breeze_buddy.lead_call_tracker import (
        abort_lead_by_id_query,
        insert_lead_call_tracker_query,
        reset_widget_voice_lead_query,
        update_lead_call_completion_details_query,
    )

    # the call's completion, the agent's outcome hook among its callers:
    # one value, both columns
    query, values = update_lead_call_completion_details_query(
        "lead-1", status=LeadCallStatus.FINISHED, outcome="BUSY"
    )
    assert values[1] == "BUSY"
    assert '"outcome" = $2' in query and '"agent_outcome" = $2' in query
    # no outcome, neither column
    query, _ = update_lead_call_completion_details_query(
        "lead-1", status=LeadCallStatus.FINISHED
    )
    assert "outcome" not in query

    query, values = abort_lead_by_id_query("lead-1", "cancelled")
    assert values[1] == "ABORT"
    assert '"outcome" = $2' in query and '"agent_outcome" = $2' in query

    # a blocked call's outcome, known at insert
    query, values = insert_lead_call_tracker_query(
        "lead-1", "r", "t", "m", None, None, None, outcome="BLOCKED_REJECT"
    )
    columns = query[query.index("(") + 1 : query.index(")")].split(",")
    placeholders = query[query.index("VALUES (") + 8 :].split(")")[0].split(",")
    position = dict(
        zip([c.strip().strip('"') for c in columns], [p.strip() for p in placeholders])
    )
    assert position["outcome"] == position["agent_outcome"] == "$19"
    assert values[18] == "BLOCKED_REJECT"

    # a widget lead reused for the next voice attachment clears both
    query, _ = reset_widget_voice_lead_query("lead-1", {}, {}, "DAILY_STREAM")
    assert '"outcome"              = NULL' in query
    assert '"agent_outcome"        = NULL' in query


@pytest.mark.parametrize("stored", [None, "BUSY"])
def test_the_lead_carries_the_agents_replaced_word(stored):
    from app.database.decoder.breeze_buddy.lead_call_tracker import (
        decode_lead_call_tracker,
    )

    row = {
        **make_lead().model_dump(exclude={"metaData", "agent_outcome"}),
        "meta_data": {},
        "status": "FINISHED",
        "execution_mode": "TELEPHONY",
        "call_direction": "OUTBOUND",
    }
    if stored is not None:
        row["agent_outcome"] = stored

    # a row from before migration 084 has no such column: None
    lead = decode_lead_call_tracker(cast(Any, row))
    assert lead is not None and lead.agent_outcome == stored
