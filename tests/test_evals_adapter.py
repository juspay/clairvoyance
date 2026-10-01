"""The eval adapter: dispatch, gating and storage.

Companion to tests/test_evals_evaluation.py — these tests cover
the run-time half (engine dispatch, channel gating, result storage) and
travel with evals/evaluator.py. Nothing calls the adapter yet;
the call site is a separate decision.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from app.ai.voice.agents.breeze_buddy.services.evals import (
    evaluator,
)
from app.ai.voice.agents.breeze_buddy.services.evals.engines.common import (
    Verdict,
)
from app.schemas.breeze_buddy.conversation_analysis import (
    ConversationChannel,
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
