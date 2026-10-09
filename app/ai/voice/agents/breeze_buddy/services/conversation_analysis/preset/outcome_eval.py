"""The outcome check: the built-in outcome_correctness eval, at the end of a
telephony call.

Before a finished call's one call.completed is sent (``crm_mirror``'s
finished tap, already in the background), Jev is asked which of the
agent's own outcome words the call should have ended with
(``app/services/evals/preset/outcome_correctness/``). A confident answer that
differs from the agent's word becomes the lead's outcome and the event's.
The eval gets at most ``_MAX_WAIT_SECONDS``; past it the agent's word goes
out. A check that ran past it, or failed, leaves a FAILED result row (saved
in the background) so outcome analytics can count it.
"""

import asyncio
import time
from typing import Any, Dict, Optional

from app.core.concurrency import spawn_background_task
from app.core.logger import logger
from app.database.accessor.breeze_buddy.evaluation_config import (
    get_outcome_correctness,
)
from app.database.accessor.breeze_buddy.evaluation_result import (
    save_evaluation_failure,
)
from app.database.accessor.breeze_buddy.lead_call_tracker import set_eval_outcome
from app.database.accessor.breeze_buddy.template import get_template_by_id
from app.schemas import ExecutionMode, LeadCallTracker
from app.schemas.breeze_buddy.conversation_analysis import (
    ConversationChannel,
    ConversationEvaluationJob,
)
from app.schemas.breeze_buddy.evals import EvaluationType
from app.services.evals.engines.common import ChoiceResult
from app.services.evals.evaluator import analyze_evals
from app.services.evals.preset.outcome_correctness import (
    NO_DECISION,
    corrected_outcome,
    outcome_answer,
    outcome_question,
    replaceable,
)
from app.services.evals.shared.utils.extract_agent_outcomes import (
    extract_agent_outcomes,
)
from app.utils.common import parse_json

from ..worker import get_analysis_context

_MAX_WAIT_SECONDS = 2

#: The FAILED row's error for a check that ran past ``_MAX_WAIT_SECONDS``;
#: analytics tell timeouts from other failures by this prefix.
TIMED_OUT = "TIMEOUT"

#: Every outcome-check log line carries this, so one filter finds them all.
LOG_COMPONENT = "buddy.outcome_eval"


def _log_mismatch(
    lead: LeadCallTracker,
    answer: ChoiceResult,
    settings: Dict[str, Any],
    result: str,
) -> None:
    """One line for a call whose eval answer differs from the agent's word:
    the call's ids and the two outcomes. ``result``: what the check did about
    it."""
    # the enrollment only when a workflow run placed the call
    run = {"enrollment_id": lead.enrollment_id} if lead.enrollment_id else {}
    logger.bind(
        component=LOG_COMPONENT,
        lead_id=lead.id,
        call_sid=lead.call_id,
        **run,
        agent_outcome=lead.outcome,
        eval_outcome=answer.value,
        eval_confidence=answer.confidence,
        min_confidence=settings.get("min_confidence"),
        result=result,
    ).info(
        f"Outcome mismatch for {lead.id}: agent {lead.outcome!r}, eval "
        f"{answer.value!r} (confidence {answer.confidence}): {result}"
    )


def _save_failure(builtin: Dict[str, Any], context: Dict[str, Any], error: str) -> None:
    """This call's FAILED outcome_correctness row, in the background:
    call.completed never waits on it. One per call (the insert dedupes).
    Never raises: the outcome check must not."""
    try:
        spawn_background_task(
            save_evaluation_failure(
                str(builtin["id"]),
                EvaluationType.CONVERSATION_EVALS.value,
                str(context["source_id"]),
                str(context["reseller_id"]),
                context.get("merchant_id"),
                str(context["template_id"]),
                context["started_at"],
                error,
            ),
            name=f"outcome-eval-failure-{context.get('source_id')}",
        )
    except Exception as exc:
        logger.error(f"Outcome check failure row not saved: {exc!r}")


async def checked_outcome(lead: LeadCallTracker) -> Optional[str]:
    """The outcome a finished lead's call.completed carries: the built-in
    eval's correction when it is confident, differs from the agent's word
    and is saved on the lead, with the eval given at most
    ``_MAX_WAIT_SECONDS``; else the agent's word. Never raises."""
    if (
        not lead.template_id
        or not replaceable(lead.outcome)
        # a web call's bot process exits with the call, before an eval ends
        or lead.execution_mode == ExecutionMode.DAILY
    ):
        return lead.outcome
    deadline = time.monotonic() + _MAX_WAIT_SECONDS
    # set once the eval is about to run: what a FAILED row needs
    builtin: Optional[Dict[str, Any]] = None
    context: Optional[Dict[str, Any]] = None
    try:
        builtin = await get_outcome_correctness(str(lead.template_id))
        if builtin is None:
            return lead.outcome  # off for this agent
        template = await get_template_by_id(str(lead.template_id))
        words = extract_agent_outcomes(template) if template else None
        if not words:
            logger.info(
                f"Outcome check skipped for {lead.id}: template "
                f"{lead.template_id} writes no outcome word literally"
            )
            return lead.outcome
        context = await get_analysis_context(
            ConversationEvaluationJob(
                source_id=lead.id,
                channel=ConversationChannel.VOICE,
                template_id=lead.template_id,  # type: ignore[arg-type]
            )
        )
        if context is None:
            return lead.outcome
        settings = parse_json(builtin, "configuration") or {}
        # the row holds the engine, model and threshold; the question is this
        # agent's, built per call
        evaluation = {
            **builtin,
            "configuration": {
                "engine": settings["engine"],
                "provider": settings["provider"],
                "model": settings["model"],
                "questions": [outcome_question(words)],
            },
        }
        # what the eval judge sees beside the transcript
        judged = {
            **context,
            "channel": ConversationChannel.VOICE.value,
            "payload": lead.payload or {},
            "meta_data": lead.metaData or {},
            "recorded_outcome": lead.outcome,
        }
        verdict = await asyncio.wait_for(
            analyze_evals(judged, evaluation, ConversationChannel.VOICE),
            timeout=max(0.0, deadline - time.monotonic()),
        )
        if verdict is None:
            # analyze_evals logged why; nothing was stored
            _save_failure(builtin, context, "EVAL_FAILED")
            return lead.outcome
        answer = outcome_answer(verdict)
        differs = bool(
            answer
            and answer.value
            and answer.value.casefold() != (lead.outcome or "").casefold()
        )
        corrected = corrected_outcome(verdict, lead.outcome, settings["min_confidence"])
        if corrected is None:
            if answer and differs:
                _log_mismatch(
                    lead,
                    answer,
                    settings,
                    (
                        "kept: no decision"
                        if answer.value == NO_DECISION
                        else "kept: not confident enough"
                    ),
                )
            return lead.outcome
        # the outcome alone: agent_outcome keeps the agent's word it replaced
        updated = await set_eval_outcome(lead.id, corrected, lead.outcome)
        if answer:
            _log_mismatch(
                lead, answer, settings, "replaced" if updated else "kept: not saved"
            )
        if updated is None:
            logger.error(
                f"Outcome check {lead.id}: outcome {corrected!r} not saved, "
                f"call.completed keeps {lead.outcome!r}"
            )
            return lead.outcome
        return updated.outcome
    except asyncio.TimeoutError:
        logger.warning(
            f"Outcome check {lead.id} ran past {_MAX_WAIT_SECONDS}s: "
            f"call.completed keeps {lead.outcome!r}"
        )
        if builtin is not None and context is not None:
            _save_failure(
                builtin, context, f"{TIMED_OUT}: ran past {_MAX_WAIT_SECONDS}s"
            )
    except Exception as exc:
        logger.error(f"Outcome check {lead.id} failed: {exc!r}")
    return lead.outcome
