"""The eval adapter: dispatch, gating and storage.

Companion to tests/test_evals_evaluation.py — these tests cover
the run-time half (engine dispatch, channel gating, result storage, and
each step on its own) and travel with evals/evaluator.py. Nothing calls the adapter yet;
the call site is a separate decision.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import cast
from unittest.mock import AsyncMock

import pytest

from app.schemas.breeze_buddy.conversation_analysis import (
    ConversationChannel,
)
from app.services.evals import (
    evaluator,
)
from app.services.evals.engines.base import EvalEngine
from app.services.evals.engines.common import (
    Verdict,
)
from tests.test_evals_evaluation import (
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
        self.verdict = verdict or Verdict(
            engine="fake", provider="typesafe", model="m", result=[]
        )
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

    await evaluator.analyze_evals(_context(), _evaluation(), ConversationChannel.VOICE)

    assert engine.calls == 1
    save.assert_awaited_once()
    assert save.await_args is not None
    args = save.await_args.args
    assert args[0] == "00000000-0000-0000-0000-000000000010"
    assert args[1] == "CONVERSATION_EVALS"  # from the row, not from the package
    assert args[2] == "lead-1"
    # one verdict = a one-element array; the adapter stamps ``type`` (the
    # row's evaluation type) for the result column and its identity CHECK —
    # the verdict itself carries none
    assert args[7] == [{"type": "CONVERSATION_EVALS", **engine.verdict.model_dump()}]


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
    await evaluator.analyze_evals(_context(), evaluation, ConversationChannel.VOICE)
    assert seen["configuration"] == {"engine": "fake", "provider": "typesafe"}


async def test_adapter_skips_unknown_engine(monkeypatch):
    save = AsyncMock()
    monkeypatch.setattr(evaluator, "save_evaluation_results", save)
    await evaluator.analyze_evals(
        _context(), _evaluation("nope"), ConversationChannel.VOICE
    )
    save.assert_not_awaited()


async def test_adapter_gates_on_channel(monkeypatch):
    engine = FakeEngine()
    save = AsyncMock()
    monkeypatch.setitem(evaluator.ENGINES, "fake", engine)
    monkeypatch.setattr(evaluator, "save_evaluation_results", save)
    await evaluator.analyze_evals(_context(), _evaluation(), ConversationChannel.CHAT)
    assert engine.calls == 0
    save.assert_not_awaited()


async def test_adapter_failure_logs_and_skips(monkeypatch):
    engine = FakeEngine(error=RuntimeError("vendor down"))
    save = AsyncMock()
    monkeypatch.setitem(evaluator.ENGINES, "fake", engine)
    monkeypatch.setattr(evaluator, "save_evaluation_results", save)

    # must not raise — read-only analytics never breaks the worker
    await evaluator.analyze_evals(_context(), _evaluation(), ConversationChannel.VOICE)
    assert engine.calls == 1  # one attempt; retries live in the provider
    save.assert_not_awaited()


# --- the steps, each callable on its own ------------------------------------


def test_get_engine_resolves_or_refuses():
    assert evaluator.get_engine("prompt") is evaluator.ENGINES["prompt"]
    assert evaluator.get_engine("structured") is evaluator.ENGINES["structured"]
    for bad in ("nope", None, ["prompt"]):
        with pytest.raises(ValueError, match="unknown engine"):
            evaluator.get_engine(bad)


async def test_run_evaluation_returns_the_verdict_and_stores_nothing(monkeypatch):
    # a caller that only wants the verdict: no DB, no channel gate
    engine = FakeEngine()
    save = AsyncMock()
    monkeypatch.setattr(evaluator, "save_evaluation_results", save)

    verdict = await evaluator.run_evaluation(
        cast(EvalEngine, engine), _context(), {"engine": "fake"}
    )

    assert verdict is engine.verdict
    assert engine.calls == 1
    save.assert_not_awaited()


async def test_run_evaluation_raises_so_the_caller_owns_the_fail_posture(
    monkeypatch,
):
    with pytest.raises(RuntimeError, match="vendor down"):
        await evaluator.run_evaluation(
            cast(EvalEngine, FakeEngine(error=RuntimeError("vendor down"))),
            _context(),
            {},
        )

    class SlowEngine(FakeEngine):
        async def evaluate(self, context, configuration):
            await asyncio.sleep(1)
            return self.verdict

    monkeypatch.setattr(evaluator, "_EVALUATION_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(asyncio.TimeoutError):
        await evaluator.run_evaluation(cast(EvalEngine, SlowEngine()), _context(), {})


async def test_save_verdict_stores_one_row_stamped_with_the_type(monkeypatch):
    save = AsyncMock()
    monkeypatch.setattr(evaluator, "save_evaluation_results", save)
    verdict = Verdict(engine="prompt", provider="openrouter", model="m", result=[])

    await evaluator.save_verdict("eval-id", "CONVERSATION_EVALS", _context(), verdict)

    save.assert_awaited_once()
    assert save.await_args is not None
    assert save.await_args.args == (
        "eval-id",
        "CONVERSATION_EVALS",
        "lead-1",
        "reseller",
        "merchant",
        TEMPLATE_ID,
        _context()["started_at"],
        [{"type": "CONVERSATION_EVALS", **verdict.model_dump()}],
    )
