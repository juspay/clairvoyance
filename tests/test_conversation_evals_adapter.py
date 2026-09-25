"""CONVERSATION_EVALS worker integration: the adapter and its storage.

Stash-companion to tests/test_conversation_evals_evaluation.py — these tests cover
the run-time half (adapter dispatch, engine gating, result storage, worker
wiring) and travel with worker.py / conversation_evals/evaluator.py.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.ai.voice.agents.breeze_buddy.services.conversation_analysis import worker
from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals import (
    evaluator,
)
from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.engines.base import (
    RESULT_TYPE,
)
from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.topics.evaluator import (
    ModelUnavailableError,
)
from app.schemas.breeze_buddy.conversation_analysis import (
    ConversationChannel,
    ConversationEvaluationJob,
    EvaluationType,
)
from tests.test_conversation_evals_evaluation import (
    LEAD_META_DATA,
    LEAD_PAYLOAD,
    TEMPLATE_ID,
)


def _context() -> dict:
    return {
        "source_id": "lead-1",
        "reseller_id": "reseller",
        "merchant_id": "merchant",
        "template_id": TEMPLATE_ID,
        "started_at": datetime(2026, 9, 23, tzinfo=timezone.utc),
        "transcript": [{"role": "user", "content": "hi"}],
        "payload": LEAD_PAYLOAD,
        "meta_data": LEAD_META_DATA,
        "recorded_outcome": "BUSY",
    }


class FakeEngine:
    name = "fake"
    channels = frozenset({ConversationChannel.VOICE})
    providers = {"typesafe": object()}

    def __init__(self, verdict=None, error=None):
        self.verdict = verdict or {"type": RESULT_TYPE, "engine": "fake"}
        self.error = error
        self.calls = 0

    async def evaluate(self, context, configuration):
        self.calls += 1
        if self.error:
            raise self.error
        return self.verdict


def _evaluation(engine: str = "fake") -> dict:
    return {
        "id": "00000000-0000-0000-0000-000000000010",
        "evaluation_type": "CONVERSATION_EVALS",
        "configuration": {"engine": engine, "provider": "typesafe"},
    }


async def test_adapter_runs_engine_and_stores(monkeypatch):
    engine = FakeEngine()
    save = AsyncMock()
    monkeypatch.setitem(evaluator.ENGINES, "fake", engine)
    monkeypatch.setattr(evaluator, "save_evaluation_results", save)

    await evaluator.analyze_conversation_evals(
        _context(), _evaluation(), ConversationChannel.VOICE
    )

    assert engine.calls == 1
    save.assert_awaited_once()
    assert save.await_args is not None
    args = save.await_args.args
    assert args[0] == "00000000-0000-0000-0000-000000000010"
    assert args[1] == EvaluationType.CONVERSATION_EVALS.value
    assert args[2] == "lead-1"
    assert args[7] == [engine.verdict]  # one verdict = a one-element array


async def test_adapter_decodes_jsonb_text_configuration(monkeypatch):
    # get_enabled_evaluations hands the jsonb column back as text
    engine = FakeEngine()
    seen = {}

    async def evaluate(context, configuration):
        seen["configuration"] = configuration
        return engine.verdict

    engine.evaluate = evaluate
    monkeypatch.setitem(evaluator.ENGINES, "fake", engine)
    monkeypatch.setattr(evaluator, "save_evaluation_results", AsyncMock())
    evaluation = _evaluation()
    evaluation["configuration"] = json.dumps(evaluation["configuration"])
    await evaluator.analyze_conversation_evals(
        _context(), evaluation, ConversationChannel.VOICE
    )
    assert seen["configuration"] == {"engine": "fake", "provider": "typesafe"}


async def test_adapter_skips_unknown_engine(monkeypatch):
    save = AsyncMock()
    monkeypatch.setattr(evaluator, "save_evaluation_results", save)
    await evaluator.analyze_conversation_evals(
        _context(), _evaluation("nope"), ConversationChannel.VOICE
    )
    save.assert_not_awaited()


async def test_adapter_gates_on_channel(monkeypatch):
    engine = FakeEngine()
    save = AsyncMock()
    monkeypatch.setitem(evaluator.ENGINES, "fake", engine)
    monkeypatch.setattr(evaluator, "save_evaluation_results", save)
    await evaluator.analyze_conversation_evals(
        _context(), _evaluation(), ConversationChannel.CHAT
    )
    assert engine.calls == 0
    save.assert_not_awaited()


async def test_adapter_failure_logs_and_skips(monkeypatch):
    engine = FakeEngine(error=RuntimeError("vendor down"))
    save = AsyncMock()
    monkeypatch.setitem(evaluator.ENGINES, "fake", engine)
    monkeypatch.setattr(evaluator, "save_evaluation_results", save)

    # must not raise — read-only analytics never breaks the worker
    await evaluator.analyze_conversation_evals(
        _context(), _evaluation(), ConversationChannel.VOICE
    )
    assert engine.calls == 1  # one attempt; retries live in the provider
    save.assert_not_awaited()


# --- the worker: CONVERSATION_EVALS is independent of the TOPIC pass ---------

TOPIC_ROW = {
    "id": "00000000-0000-0000-0000-000000000020",
    "evaluation_type": "TOPIC",
    "topics": [],
    "configuration": {"model": "m"},
}


def _job(deliveries: int = 0) -> ConversationEvaluationJob:
    return ConversationEvaluationJob(
        source_id="lead-1",
        channel=ConversationChannel.VOICE,
        template_id=TEMPLATE_ID,
        deliveries=deliveries,
    )


def _patch_worker(monkeypatch, rows, analyze_topics) -> dict:
    mocks = {
        "conversation_evals": AsyncMock(),
        "requeue": AsyncMock(),
        "save_failure": AsyncMock(),
    }
    monkeypatch.setattr(worker, "_consecutive_failures", 0)
    monkeypatch.setattr(worker, "_paused_until", 0.0)
    monkeypatch.setattr(worker, "get_enabled_evaluations", AsyncMock(return_value=rows))
    monkeypatch.setattr(
        worker, "get_analysis_context", AsyncMock(return_value=_context())
    )
    monkeypatch.setattr(worker, "analyze_topics", analyze_topics)
    monkeypatch.setattr(
        worker, "analyze_conversation_evals", mocks["conversation_evals"]
    )
    monkeypatch.setattr(worker, "requeue_conversation_evaluation", mocks["requeue"])
    monkeypatch.setattr(worker, "save_topic_failure", mocks["save_failure"])
    return mocks


async def test_worker_runs_conversation_evals_once_whatever_the_row_order(monkeypatch):
    topics = AsyncMock(return_value=True)
    mocks = _patch_worker(monkeypatch, [TOPIC_ROW, _evaluation()], topics)

    await worker._evaluate(_job())

    mocks["conversation_evals"].assert_awaited_once()
    assert mocks["conversation_evals"].await_args is not None
    _, row, channel = mocks["conversation_evals"].await_args.args
    assert row["evaluation_type"] == "CONVERSATION_EVALS"
    assert channel is ConversationChannel.VOICE
    topics.assert_awaited_once()


async def test_worker_conversation_evals_survives_topic_requeues_and_give_up(
    monkeypatch,
):
    # TOPIC's model is down for good: 4 requeues, then the gave-up FAILED row.
    # The engine is paid on the first delivery only and the verdict is kept —
    # it does not ride on the TOPIC outcome.
    topics = AsyncMock(side_effect=ModelUnavailableError("down"))
    mocks = _patch_worker(monkeypatch, [_evaluation(), TOPIC_ROW], topics)
    job = _job()

    for _ in range(worker._MAX_DELIVERIES):
        worker._paused_until = 0.0
        await worker._evaluate(job)

    assert job.deliveries == worker._MAX_DELIVERIES
    assert mocks["requeue"].await_count == worker._MAX_DELIVERIES - 1
    mocks["save_failure"].assert_awaited_once()
    mocks["conversation_evals"].assert_awaited_once()  # first delivery, never again


async def test_worker_conversation_evals_runs_when_topics_raise(monkeypatch):
    topics = AsyncMock(side_effect=RuntimeError("boom"))
    mocks = _patch_worker(monkeypatch, [TOPIC_ROW, _evaluation()], topics)

    with pytest.raises(RuntimeError):
        await worker._evaluate(_job())

    mocks["conversation_evals"].assert_awaited_once()


async def test_worker_conversation_evals_only_template_never_touches_topics(
    monkeypatch,
):
    topics = AsyncMock(return_value=True)
    mocks = _patch_worker(monkeypatch, [_evaluation()], topics)

    await worker._evaluate(_job())

    mocks["conversation_evals"].assert_awaited_once()
    topics.assert_not_awaited()
    mocks["requeue"].assert_not_awaited()
