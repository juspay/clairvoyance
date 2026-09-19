"""Post-conversation evaluation worker."""

import asyncio
import time
from typing import Any, Dict, List

from app.core.config.dynamic import BB_ANALYSIS_CONSUMER_COUNT
from app.core.logger import logger
from app.database.accessor.breeze_buddy.chat_session import (
    get_chat_session_by_id,
    list_chat_messages_for_session,
)
from app.database.accessor.breeze_buddy.evaluation_config import (
    get_enabled_evaluations,
)
from app.database.accessor.breeze_buddy.lead_call_tracker import get_lead_by_id
from app.schemas import LeadCallStatus
from app.schemas.breeze_buddy.chat import ChatSessionStatus
from app.schemas.breeze_buddy.conversation_analysis import (
    ConversationChannel,
    ConversationEvaluationJob,
    EvaluationType,
)

from .queue import dequeue_conversation_evaluation, requeue_conversation_evaluation
from .topics.evaluator import ModelUnavailableError, analyze_topics

_FIRST_PAUSE_SECONDS = 30
_MAX_PAUSE_SECONDS = 600

_consumer_tasks: List[asyncio.Task] = []

_consecutive_failures: int = 0
_paused_until: float = 0.0


def _enabled(metadata: Dict[str, Any], key: str) -> bool:
    return str(metadata.get(key, "false")).lower() == "true"


async def get_analysis_context(
    job: ConversationEvaluationJob,
) -> Dict[str, Any] | None:
    template_id = str(job.template_id)
    if job.channel is ConversationChannel.VOICE:
        lead = await get_lead_by_id(job.source_id)
        if (
            not lead
            or lead.template_id != template_id
            or lead.status is not LeadCallStatus.FINISHED
            or not (lead.call_initiated_time or lead.created_at)
        ):
            return None
        metadata = lead.metaData or {}
        transcript = metadata.get("transcription")
        if (
            _enabled(metadata, "is_demo")
            or _enabled(metadata, "playground")
            or str(lead.outcome or "").upper() in {"NO_ANSWER", "VOICEMAIL"}
        ):
            return None
        context = {
            "source_id": lead.id,
            "reseller_id": lead.reseller_id,
            "merchant_id": lead.merchant_id,
            "template_id": template_id,
            "started_at": lead.call_initiated_time or lead.created_at,
            "transcript": transcript,
        }
    else:
        session = await get_chat_session_by_id(job.source_id)
        if (
            not session
            or session.template_id != template_id
            or session.status is not ChatSessionStatus.ENDED
            or not session.created_at
            or _enabled(session.metadata, "demo")
            or _enabled(session.metadata, "playground")
        ):
            return None
        messages = await list_chat_messages_for_session(job.source_id)
        context = {
            "source_id": session.id,
            "reseller_id": session.reseller_id,
            "merchant_id": session.merchant_id,
            "template_id": template_id,
            "started_at": session.created_at,
            "transcript": [
                {
                    "idx": message.idx,
                    "role": message.role.value,
                    "content": message.content,
                }
                for message in messages
                if message.content and message.content.strip()
            ],
        }

    transcript = context["transcript"]
    if not isinstance(transcript, list) or not any(
        isinstance(turn, dict)
        and turn.get("role") == "user"
        and str(turn.get("content") or "").strip()
        for turn in transcript
    ):
        return None
    context["transcript"] = [
        dict(turn) for turn in transcript if isinstance(turn, dict)
    ]
    return context


async def _consume_queue(recovery_lock: asyncio.Lock) -> None:
    """Take jobs off the Redis queue and evaluate them, one at a time."""
    while True:
        try:
            job = await dequeue_conversation_evaluation()
            if _consecutive_failures == 0:
                await _evaluate(job)
                continue

            async with recovery_lock:
                await asyncio.sleep(max(0.0, _paused_until - time.monotonic()))
                await _evaluate(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Conversation analysis queue consumer failed: {exc}")
            await asyncio.sleep(1)


async def _evaluate(job: ConversationEvaluationJob) -> None:
    global _consecutive_failures, _paused_until

    evaluations = await get_enabled_evaluations(str(job.template_id))
    if not evaluations:
        return

    context = await get_analysis_context(job)
    if context is None:
        return

    for evaluation in evaluations:
        try:
            evaluation_type = EvaluationType(evaluation.get("evaluation_type"))
        except ValueError:
            logger.warning(
                f"Ignoring unsupported evaluation type for template "
                f"{job.template_id}: {evaluation.get('evaluation_type')}"
            )
            continue
        if evaluation_type is not EvaluationType.TOPIC:
            continue

        try:
            await analyze_topics(context, evaluation)
        except ModelUnavailableError as exc:
            now = time.monotonic()
            if now >= _paused_until:
                # First failure of this round. Consumers already mid-call when
                # the model went down land here microseconds apart; they are
                # one outage, not four, so only the first climbs a rung —
                # otherwise the opening pause jumps straight to minutes.
                _consecutive_failures += 1
                backoff = _FIRST_PAUSE_SECONDS * 2 ** (_consecutive_failures - 1)
                _paused_until = now + min(
                    exc.retry_after or backoff, _MAX_PAUSE_SECONDS
                )
            await requeue_conversation_evaluation(job)
            logger.error(
                f"Topic evaluation {job.source_id} MODEL_UNAVAILABLE ({exc}): "
                f"job re-queued, all consumers paused for "
                f"{_paused_until - now:.0f}s "
                f"(failure #{_consecutive_failures} in a row)"
            )
            return

    if _consecutive_failures:
        logger.info("Topic evaluation resumed after the model recovered")
        _consecutive_failures = 0
        _paused_until = 0.0


async def start_analysis_worker() -> None:
    if any(not task.done() for task in _consumer_tasks):
        return
    count = max(1, await BB_ANALYSIS_CONSUMER_COUNT())
    recovery_lock = asyncio.Lock()
    _consumer_tasks[:] = [
        asyncio.create_task(
            _consume_queue(recovery_lock),
            name=f"conversation-analysis-consumer-{index}",
        )
        for index in range(count)
    ]
    logger.info(f"Conversation analysis worker started with {count} consumers")


async def stop_analysis_worker() -> None:
    if not _consumer_tasks:
        return
    for task in _consumer_tasks:
        task.cancel()
    await asyncio.gather(*_consumer_tasks, return_exceptions=True)
    _consumer_tasks.clear()
    logger.info("Conversation analysis worker stopped")
