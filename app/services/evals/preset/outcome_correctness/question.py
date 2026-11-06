"""The outcome_correctness eval's one question, and how its answer is read.

The question is a choice among the agent's own outcome words
(``app/services/evals/shared/utils/extract_agent_outcomes.py``) plus
``NO_DECISION``. Its answer replaces the agent's word only when it is an
outcome word, sure enough and different.
"""

from typing import Any, Dict, Mapping, Optional

from app.services.evals.engines.common import ChoiceResult, Verdict

#: The option that says the call reached none of the agent's outcomes: the
#: agent's word is kept.
NO_DECISION = "NO_DECISION"
_QUESTION_KEY = "outcome"

# Outcomes the eval never replaces: they say how the call went, not what was
# decided. IVR_* likewise.
_KEPT_OUTCOMES = frozenset(
    {
        "TRANSFERRED",
        "EARLY_HANGUP",
        "UNKNOWN",
        "ENDED_BY_WIDGET",
        "NO_ANSWER",
        "VOICEMAIL",
    }
)


def replaceable(outcome: Optional[str]) -> bool:
    """Whether the eval may replace this outcome."""
    word = (outcome or "").upper()
    return word not in _KEPT_OUTCOMES and not word.startswith("IVR_")


def outcome_question(words: Mapping[str, str]) -> Dict[str, Any]:
    """The one question the eval asks: a choice among the agent's own
    outcome words, or none of them."""
    return {
        "key": _QUESTION_KEY,
        "label": "Outcome",
        "type": "choice",
        "instructions": (
            "Which outcome should this call have ended with, judging only by "
            "what the customer and the agent said?"
        ),
        "criteria": {
            **words,
            NO_DECISION: "The conversation reached none of the outcomes above.",
        },
    }


def outcome_answer(verdict: Optional[Verdict]) -> Optional[ChoiceResult]:
    """The eval's answer to its outcome question, if it gave one."""
    return next(
        (
            result
            for result in (verdict.result if verdict else [])
            if isinstance(result, ChoiceResult) and result.key == _QUESTION_KEY
        ),
        None,
    )


def corrected_outcome(
    verdict: Optional[Verdict], recorded: Optional[str], min_confidence: float
) -> Optional[str]:
    """The word the eval puts in place of ``recorded``: its answer when that
    is an outcome word, at least ``min_confidence`` sure and different;
    else None (the recorded word stands)."""
    answer = outcome_answer(verdict)
    if (
        answer is None
        or not answer.value
        or answer.value == NO_DECISION
        or answer.confidence is None
        or answer.confidence < min_confidence
        or (recorded or "").casefold() == answer.value.casefold()
    ):
        return None
    return answer.value
