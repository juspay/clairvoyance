"""Post-conversation evaluation worker."""

import asyncio
from typing import Any, Dict

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

from .queue import dequeue_conversation_evaluation
from .topics.evaluator import analyze_topics

_consumer_task: asyncio.Task | None = None


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


async def _consume_queue() -> None:
    """Consume Redis jobs sequentially."""
    while True:
        try:
            job = await dequeue_conversation_evaluation()
            await _evaluate(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Conversation analysis queue consumer failed: {exc}")
            await asyncio.sleep(1)


async def _evaluate(job: ConversationEvaluationJob) -> None:
    # Only post-conversation evaluation types belong in this worker. Real-time
    # Guardrails use the same config table but execute inside the voice pipeline.
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
        if evaluation_type is EvaluationType.TOPIC:
            await analyze_topics(context, evaluation)


async def start_analysis_worker() -> None:
    global _consumer_task
    if _consumer_task is not None and not _consumer_task.done():
        return
    _consumer_task = asyncio.create_task(
        _consume_queue(), name="conversation-analysis-consumer"
    )
    logger.info("Conversation analysis worker started")


async def stop_analysis_worker() -> None:
    global _consumer_task
    if _consumer_task is None:
        return
    _consumer_task.cancel()
    await asyncio.gather(_consumer_task, return_exceptions=True)
    _consumer_task = None
    logger.info("Conversation analysis worker stopped")
