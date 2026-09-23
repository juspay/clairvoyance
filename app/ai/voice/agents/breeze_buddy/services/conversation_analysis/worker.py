"""Post-conversation evaluation worker."""

import asyncio
import time
from typing import Any, Dict, List

from app.core.config.dynamic import BB_ANALYSIS_CONSUMER_COUNT
from app.core.logger import logger
from app.core.logger.context import (
    clear_log_context,
    set_log_context,
    update_log_context,
)
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

from .queue import (
    LOG_COMPONENT,
    dequeue_conversation_evaluation,
    requeue_conversation_evaluation,
)
from .topics.evaluator import (
    ModelUnavailableError,
    analyze_topics,
    save_topic_failure,
)

_FIRST_PAUSE_SECONDS = 30
_MAX_PAUSE_SECONDS = 600
_OUTAGE_CHECK_SECONDS = 5.0
_MAX_DELIVERIES = 5

_consumer_tasks: List[asyncio.Task] = []

_consecutive_failures: int = 0
_paused_until: float = 0.0

# Evaluations running on this pod right now, for each job's started line.
_in_flight: int = 0


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


def _in_outage() -> bool:
    """Re-read the shared outage state. Another consumer can change it across
    any await, so this is a call rather than a name a checker may narrow."""
    return _consecutive_failures > 0


async def _consume_queue(recovery_lock: asyncio.Lock) -> None:
    """Take jobs off the Redis queue and evaluate them, one at a time."""
    while True:
        # The previous job's ids must not leak onto this one's lines.
        clear_log_context()
        job = None
        try:
            if _in_outage():
                async with recovery_lock:
                    if _in_outage():
                        while _in_outage():
                            remaining = _paused_until - time.monotonic()
                            if remaining <= 0:
                                break
                            await asyncio.sleep(min(_OUTAGE_CHECK_SECONDS, remaining))
                        if not _in_outage():
                            continue
                        job = await dequeue_conversation_evaluation()
                        await asyncio.sleep(max(0.0, _paused_until - time.monotonic()))
                        await _evaluate(job)
                        continue

            job = await dequeue_conversation_evaluation()
            if _in_outage():
                await requeue_conversation_evaluation(job)
                continue
            await _evaluate(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            where = f" for {job.source_id} (template {job.template_id})" if job else ""
            logger.bind(component=LOG_COMPONENT).error(
                f"Conversation analysis queue consumer failed{where}: {exc}"
            )
            await asyncio.sleep(1)


async def _evaluate(job: ConversationEvaluationJob) -> None:
    global _consecutive_failures, _paused_until, _in_flight

    set_log_context(
        component=LOG_COMPONENT,
        source_id=job.source_id,
        template_id=str(job.template_id),
        channel=job.channel.value,
    )
    evaluations = await get_enabled_evaluations(str(job.template_id))
    if not evaluations:
        return

    context = await get_analysis_context(job)
    if context is None:
        return
    update_log_context(merchant_id=context.get("merchant_id"))

    _in_flight += 1
    try:
        logger.bind(
            in_flight=_in_flight,
            consumers=len(_consumer_tasks),
            queue_wait_s=round(time.time() - job.enqueued_at, 1),
            deliveries=job.deliveries,
            transcript_turns=len(context["transcript"]),
        ).info(f"Topic evaluation {job.source_id} started")

        model_answered = False
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
                if await analyze_topics(context, evaluation):
                    model_answered = True
            except ModelUnavailableError as exc:
                now = time.monotonic()
                if now >= _paused_until:
                    _consecutive_failures += 1
                    backoff = _FIRST_PAUSE_SECONDS * 2 ** (_consecutive_failures - 1)
                    _paused_until = now + min(
                        max(backoff, exc.retry_after or 0), _MAX_PAUSE_SECONDS
                    )
                job.deliveries += 1
                if job.deliveries >= _MAX_DELIVERIES:
                    await save_topic_failure(
                        context,
                        evaluation,
                        f"MODEL_UNAVAILABLE after {job.deliveries} deliveries: {exc}",
                    )
                    logger.bind(outcome="gave_up", deliveries=job.deliveries).error(
                        f"Topic evaluation {job.source_id} gave up after "
                        f"{job.deliveries} deliveries: FAILED row saved"
                    )
                    return
                await requeue_conversation_evaluation(job)
                logger.bind(
                    outcome="requeued",
                    deliveries=job.deliveries,
                    paused_for_s=round(_paused_until - now),
                    consecutive_failures=_consecutive_failures,
                ).error(
                    f"Topic evaluation {job.source_id} MODEL_UNAVAILABLE ({exc}): "
                    f"job re-queued (delivery {job.deliveries}), all consumers paused for "
                    f"{_paused_until - now:.0f}s "
                    f"(failure #{_consecutive_failures} in a row)"
                )
                return

        if model_answered and _consecutive_failures:
            logger.info("Topic evaluation resumed after the model recovered")
            _consecutive_failures = 0
            _paused_until = 0.0
    finally:
        _in_flight -= 1


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
