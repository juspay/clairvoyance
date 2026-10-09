"""An agent's own evals for a finished call, run by the post-call worker in
the call's job, with its topics (worker.py). The job is queued once the
end-of-call outcome check is done (crm_mirror's finished tap), so the judge
sees the final outcome.

The evals run in batches, one judge request per engine and model
(``app/services/evals/custom/batch.py``). Each eval whose verdict is stored is
marked on the job (``job.evals_done``). A batch the provider failed (a
``ProviderError``: it has already retried what it could, and a missing key or
URL never succeeds) gets a FAILED result row per eval at once and is not run
again. Any other failure puts the job back at the head of the queue, up to its
last delivery; a retry runs only the evals not marked, so none is judged or
stored twice. After the last one, each eval still failing gets a FAILED result
row (``save_eval_failures``).

They only store results: no custom eval changes the call's outcome (the
end-of-call outcome check, conversation_analysis/preset/outcome_eval.py,
is the one eval that may).

Voice only for now; chat sessions can run the same evals later.
"""

import asyncio
from typing import Any, Dict, List, Mapping, Optional

from app.core.logger import logger
from app.database.accessor.breeze_buddy.evaluation_result import (
    save_evaluation_failure,
)
from app.schemas.breeze_buddy.conversation_analysis import (
    ConversationChannel,
    ConversationEvaluationJob,
)
from app.services.evals.custom.batch import agent_evals, batches, run_batch
from app.services.model_provider import ProviderError

from ..queue import EVALS_LOG_COMPONENT


async def run_agent_evals(
    job: ConversationEvaluationJob,
    context: Dict[str, Any],
    evaluations: List[Dict[str, Any]],
) -> List[Mapping[str, Any]]:
    """Run the agent's own evals on ``context`` (the worker's), but those the
    job has marked passed. Marks each one stored on ``job.evals_done``, a
    verdict or, for a batch the provider failed, a FAILED row; returns the
    others that failed, for the worker to retry the job."""
    log = logger.bind(component=EVALS_LOG_COMPONENT, deliveries=job.deliveries)
    rows = [
        row for row in agent_evals(evaluations) if row["name"] not in job.evals_done
    ]
    if not rows or job.channel is not ConversationChannel.VOICE:
        return []
    log.info(f"Agent evals for {job.source_id} started: {[r['name'] for r in rows]}")
    failed: List[Mapping[str, Any]] = []
    for batch in batches(rows):
        try:
            await run_batch(batch, context, job.channel, job.evals_done)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            names = [row["name"] for row in batch]
            log.error(f"Agent evals {names} for {job.source_id} failed: {exc!r}")
            # a batch can fail part way through storing: what it stored passed
            failed_evals = [row for row in batch if row["name"] not in job.evals_done]
            if isinstance(exc, ProviderError):
                # the provider already retried what it could: FAILED rows now,
                # and the job never runs these again
                await save_eval_failures(
                    job, context, failed_evals, f"provider error: {exc}"
                )
                job.evals_done.extend(row["name"] for row in failed_evals)
            else:
                failed.extend(failed_evals)
    if not failed:
        log.info(f"Agent evals for {job.source_id} completed")
    return failed


async def save_eval_failures(
    job: ConversationEvaluationJob,
    context: Dict[str, Any],
    failed: List[Mapping[str, Any]],
    error: Optional[str] = None,
) -> None:
    """A FAILED row for each eval in ``failed``: after the job's last delivery,
    or at once with ``error`` for a batch the provider failed."""
    error = error or f"failed after {job.deliveries} deliveries"
    for row in failed:
        await save_evaluation_failure(
            str(row["id"]),
            str(row["evaluation_type"]),
            str(context["source_id"]),
            str(context["reseller_id"]),
            context.get("merchant_id"),
            str(context["template_id"]),
            context["started_at"],
            error,
        )
    logger.bind(
        component=EVALS_LOG_COMPONENT, outcome="gave_up", deliveries=job.deliveries
    ).error(
        f"Agent evals {[row['name'] for row in failed]} for {job.source_id} gave "
        f"up ({error}): FAILED rows saved"
    )
