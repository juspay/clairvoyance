"""The built-in outcome_correctness eval: the one question it asks, how its
answer is read, and how its verdict is stored."""

from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

from app.schemas.breeze_buddy.conversation_analysis import ConversationChannel
from app.services.evals import evaluator
from app.services.evals.engines.common import ChoiceResult, Verdict
from app.services.evals.preset.outcome_correctness import (
    NO_DECISION,
    OUTCOME_CORRECTNESS,
    corrected_outcome,
    outcome_question,
    replaceable,
)

# ---------------------------------------------------------------------------
# the question and its answer
# ---------------------------------------------------------------------------


def test_the_question_offers_the_words_and_no_decision():
    question = outcome_question({"CONFIRM": "said yes", "CANCEL": "said no"})

    assert question["type"] == "choice"
    assert question["criteria"] == {
        "CONFIRM": "said yes",
        "CANCEL": "said no",
        NO_DECISION: "The conversation reached none of the outcomes above.",
    }


def _verdict(value: Optional[str], confidence: Optional[float]) -> Verdict:
    return Verdict(
        engine="structured",
        provider="typesafe",
        model="jev",
        result=[
            ChoiceResult(
                key="outcome", label="Outcome", value=value, confidence=confidence
            )
        ],
    )


@pytest.mark.parametrize(
    "recorded, value, confidence, corrected",
    [
        ("CANCEL", "CONFIRM", 0.9, "CONFIRM"),  # confident and different
        ("CANCEL", "CONFIRM", 0.8, "CONFIRM"),  # the threshold counts
        ("BUSY", "CONFIRM", 0.9, "CONFIRM"),  # a fallback BUSY
        (None, "CONFIRM", 0.9, "CONFIRM"),  # nobody decided
        ("CANCEL", "CONFIRM", 0.79, None),  # not confident enough
        ("CANCEL", "CONFIRM", None, None),  # no confidence given
        ("confirm", "CONFIRM", 0.99, None),  # the same word
        ("CANCEL", NO_DECISION, 0.99, None),  # none of the outcomes
        ("CANCEL", None, 0.99, None),  # the question was not answered
    ],
)
def test_corrected_outcome(recorded, value, confidence, corrected):
    assert corrected_outcome(_verdict(value, confidence), recorded, 0.8) == corrected


def test_no_verdict_corrects_nothing():
    assert corrected_outcome(None, "CANCEL", 0.8) is None


@pytest.mark.parametrize(
    "outcome, replaceable_",
    [
        ("CANCEL", True),
        ("BUSY", True),  # the fallback word, kept replaceable
        (None, True),  # nobody decided
        ("TRANSFERRED", False),
        ("ended_by_widget", False),
        ("NO_ANSWER", False),
        ("IVR_LOOP_GUARD", False),
        ("IVR_SOMETHING_NEW", False),  # every IVR_* outcome
    ],
)
def test_replaceable(outcome, replaceable_):
    assert replaceable(outcome) is replaceable_


# ---------------------------------------------------------------------------
# storage: one result per call per eval
# ---------------------------------------------------------------------------


async def test_a_named_evals_verdict_is_stored_under_its_name(monkeypatch):
    verdict = _verdict("CONFIRM", 0.9)

    class Engine:
        name = "fake"
        channels = frozenset({ConversationChannel.VOICE})

        async def evaluate(self, context: Any, configuration: Any) -> Verdict:
            return verdict

    save = AsyncMock()
    monkeypatch.setitem(evaluator.ENGINES, "fake", Engine())
    monkeypatch.setattr(evaluator, "save_evaluation_results", save)
    context = {
        "source_id": "lead-1",
        "reseller_id": "r",
        "merchant_id": "m",
        "template_id": "t",
        "started_at": datetime(2026, 10, 8, tzinfo=timezone.utc),
    }
    row = {
        "id": "builtin-id",
        "evaluation_type": "CONVERSATION_EVALS",
        "name": OUTCOME_CORRECTNESS,
        "configuration": {"engine": "fake"},
    }

    assert await evaluator.analyze_evals(context, row, ConversationChannel.VOICE) == (
        verdict
    )

    assert save.await_args is not None
    args = save.await_args.args
    assert (args[0], args[1]) == ("builtin-id", "CONVERSATION_EVALS")
    assert args[7] == [{"type": OUTCOME_CORRECTNESS, **verdict.model_dump()}]
