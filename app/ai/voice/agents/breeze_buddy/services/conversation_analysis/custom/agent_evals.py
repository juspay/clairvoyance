"""An agent's own evals for a finished call, run by the post-call worker in
the call's job, after its topics (worker.py). The job is queued once the
end-of-call outcome check is done (crm_mirror's finished tap; a Daily call's
by end_conversation, as no check runs on one), so the judge sees the final
outcome.

The evals run in batches, one judge request per engine and model
(``app/services/evals/custom/batch.py``). If a batch fails, the worker puts the
whole job back at the head of the queue, up to its last delivery; each
delivery already includes the provider's own retries. A retried job runs only
the evals with no stored result yet, so a batch that succeeded on an earlier
delivery is neither judged nor stored twice. After the last delivery each
eval still failing, and with no stored result, gets a FAILED result row
(``save_eval_failures``).

They only store results: no custom eval changes the call's outcome (the
end-of-call outcome check, conversation_analysis/preset/outcome_eval.py,
is the one eval that may).

Voice only for now; chat sessions can run the same evals later.
"""

import asyncio
from typing import Any, Dict, List, Mapping

from app.core.logger import logger
from app.database.accessor.breeze_buddy.evaluation_result import (
    get_completed_eval_names,
    save_evaluation_failure,
)
from app.schemas.breeze_buddy.conversation_analysis import (
    ConversationChannel,
    ConversationEvaluationJob,
)
from app.services.evals.custom.batch import agent_evals, batches, run_batch

from ..queue import EVALS_LOG_COMPONENT


async def run_agent_evals(
    job: ConversationEvaluationJob,
    context: Dict[str, Any],
    evaluations: List[Dict[str, Any]],
) -> List[Mapping[str, Any]]:
    """Run the agent's own evals on ``context`` (the worker's); the evals
    whose batch failed, for the worker to retry the job."""
    log = logger.bind(component=EVALS_LOG_COMPONENT, deliveries=job.deliveries)
    rows = agent_evals(evaluations)
    if not rows or job.channel is not ConversationChannel.VOICE:
        return []
    if job.deliveries:
        # a retry: the evals stored on an earlier delivery are done
        done = await get_completed_eval_names(
            str(context["source_id"]), [str(row["name"]) for row in rows]
        )
        rows = [row for row in rows if row["name"] not in done]
        if not rows:
            return []
    names = [row["name"] for row in rows]
    log.info(f"Agent evals for {job.source_id} started: {names}")
    failed: List[Mapping[str, Any]] = []
    for batch in batches(rows):
        try:
            await run_batch(batch, context, job.channel)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            batch_names = [row["name"] for row in batch]
            log.error(f"Agent evals {batch_names} for {job.source_id} failed: {exc!r}")
            failed.extend(batch)
    if not failed:
        log.info(f"Agent evals for {job.source_id} completed")
    return failed


async def save_eval_failures(
    job: ConversationEvaluationJob,
    context: Dict[str, Any],
    failed: List[Mapping[str, Any]],
) -> None:
    """After the job's last delivery: a FAILED row for each eval in
    ``failed``."""
    # a batch can fail part way through storing: an eval it did store is done,
    # and is never also counted as failed
    done = await get_completed_eval_names(
        str(context["source_id"]), [str(row["name"]) for row in failed]
    )
    for row in failed:
        if row["name"] in done:
            continue
        await save_evaluation_failure(
            str(row["id"]),
            str(row["evaluation_type"]),
            str(context["source_id"]),
            str(context["reseller_id"]),
            context.get("merchant_id"),
            str(context["template_id"]),
            context["started_at"],
            f"failed after {job.deliveries} deliveries",
        )
    logger.bind(
        component=EVALS_LOG_COMPONENT, outcome="gave_up", deliveries=job.deliveries
    ).error(
        f"Agent evals for {job.source_id} gave up after {job.deliveries} "
        f"deliveries: FAILED rows saved"
    )
