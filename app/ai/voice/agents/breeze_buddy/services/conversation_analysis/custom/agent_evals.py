"""An agent's own evals for a finished call: the post-call worker's "evals"
job, queued by ``queue.enqueue_call_evaluations`` once the call's outcome is
final.

The evals run in batches, one judge request per engine and model
(``app/services/evals/custom/batch.py``). If a batch fails, the whole job goes back
to the end of the queue, up to ``_MAX_DELIVERIES`` deliveries; each delivery
already includes the provider's own retries. A call stores one result per
eval, so a batch that succeeded on an earlier delivery is not stored twice.
After the last delivery each eval still failing gets a FAILED result row.

Voice only for now; chat sessions can run the same evals later.
"""

# Not live yet: the agent's own evals (merchant evals) come next, and this
# is kept for them. See conversation_analysis/queue.py for what enabling
# them takes.
#
# import asyncio
# from typing import Any, Dict, List, Mapping
#
# from app.core.logger import logger
# from app.database.accessor.breeze_buddy.evaluation_result import (
#     save_evaluation_failure,
# )
# from app.schemas.breeze_buddy.conversation_analysis import (
#     ConversationChannel,
#     ConversationEvaluationJob,
# )
# from app.services.evals.custom.batch import agent_evals, batches, run_batch
#
# from ..queue import EVALS_LOG_COMPONENT, requeue_evals
#
# _MAX_DELIVERIES = 5
#
#
# async def run_agent_evals(
#     job: ConversationEvaluationJob,
#     context: Dict[str, Any],
#     evaluations: List[Dict[str, Any]],
# ) -> None:
#     """Run the agent's own evals on ``context`` (the worker's), retrying the
#     job on a failed batch and recording a failure after the last try."""
#     log = logger.bind(component=EVALS_LOG_COMPONENT, deliveries=job.deliveries)
#     rows = agent_evals(evaluations)
#     if not rows or job.channel is not ConversationChannel.VOICE:
#         return
#     failed: List[Mapping[str, Any]] = []
#     for batch in batches(rows):
#         try:
#             await run_batch(batch, context, job.channel)
#         except asyncio.CancelledError:
#             raise
#         except Exception as exc:
#             names = [row["name"] for row in batch]
#             log.error(f"Agent evals {names} for {job.source_id} failed: {exc!r}")
#             failed.extend(batch)
#     if not failed:
#         log.info(f"Agent evals for {job.source_id} completed")
#         return
#     deliveries = job.deliveries + 1
#     if deliveries < _MAX_DELIVERIES:
#         await requeue_evals(job.model_copy(update={"deliveries": deliveries}))
#         log.warning(f"Agent evals for {job.source_id} re-queued (try {deliveries})")
#         return
#     for row in failed:
#         await save_evaluation_failure(
#             str(row["id"]),
#             str(row["evaluation_type"]),
#             str(context["source_id"]),
#             str(context["reseller_id"]),
#             context.get("merchant_id"),
#             str(context["template_id"]),
#             context["started_at"],
#             f"failed after {deliveries} deliveries",
#         )
#     log.error(
#         f"Agent evals for {job.source_id} gave up after {deliveries} "
#         f"deliveries: FAILED rows saved"
#     )
