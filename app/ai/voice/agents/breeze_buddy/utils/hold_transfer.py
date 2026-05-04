"""
Redis pub/sub helpers for hold & consultative transfer.

Unlike warm transfer (which needs Redis state keys for provider callbacks),
hold & consult passes all metadata through the lead payload. The only Redis
operation is pub/sub for real-time result delivery between pods.
"""

import asyncio
import json
from typing import Any, Dict, List, Optional, cast

from openai.types.chat import ChatCompletionMessageParam
from pipecat.adapters.services.open_ai_adapter import OpenAILLMInvocationParams

from app.ai.voice.agents.breeze_buddy.llm import _resolve_azure
from app.core.logger import logger
from app.services.redis.client import get_redis_service


async def publish_hold_transfer_result(channel: str, result: Dict[str, Any]) -> None:
    """Publish result to a pub/sub channel.

    No-op if no subscribers — Redis pub/sub is fire-and-forget.
    """
    redis_service = await get_redis_service()
    redis_client = await redis_service.get_client()
    await redis_client.publish(channel, json.dumps(result))  # type: ignore[union-attr]
    logger.info(f"[hold_transfer] Published result to channel '{channel}'")


async def subscribe_and_wait(
    channel: str, timeout_seconds: int = 180
) -> Optional[Dict[str, Any]]:
    """Subscribe to a pub/sub channel, wait for one message, then unsubscribe.

    Returns the parsed message dict, or None on timeout / error.
    """
    redis_service = await get_redis_service()
    redis_client = await redis_service.get_client()
    pubsub = redis_client.pubsub()  # type: ignore[union-attr]
    await pubsub.subscribe(channel)
    logger.info(
        f"[hold_transfer] Subscribed to '{channel}', "
        f"waiting up to {timeout_seconds}s"
    )

    try:
        result = await asyncio.wait_for(
            _wait_for_message(pubsub),
            timeout=timeout_seconds,
        )
        if result:
            logger.info(
                f"[hold_transfer] Received result on '{channel}': "
                f"status={result.get('status')}"
            )
        return result
    except asyncio.TimeoutError:
        logger.warning(
            f"[hold_transfer] Timed out after {timeout_seconds}s on '{channel}'"
        )
        return None
    except Exception as e:
        logger.error(f"[hold_transfer] Error waiting on '{channel}': {e}")
        return None
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()


async def summarize_transcription(
    transcription: List[Dict[str, Any]],
    reason: str = "",
) -> Optional[str]:
    """Summarize a hold-transfer outbound transcription using Azure OpenAI.

    Args:
        transcription: List of {role, content} dicts (user/assistant turns).
        reason: Optional reason for the outbound call, included in the prompt.

    Returns:
        Summary string, or None on failure (caller should fall back to raw
        transcription).
    """
    if not transcription:
        return None

    try:
        llm = await _resolve_azure(None)

        conversation_text = "\n".join(
            f"{m['role']}: {m['content']}" for m in transcription if m.get("content")
        )

        system_prompt = (
            "Summarize this phone conversation concisely. "
            "Focus on key outcomes, decisions, and any actionable information "
            "that was discussed."
        )
        if reason:
            system_prompt += f"\n\nReason for call: {reason}"

        params = OpenAILLMInvocationParams(  # type: ignore[call-overload]
            messages=cast(
                list[ChatCompletionMessageParam],
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": conversation_text},
                ],
            )
        )
        chunks = await llm.get_chat_completions(params)  # type: ignore[arg-type]
        parts = [
            chunk.choices[0].delta.content
            async for chunk in chunks
            if chunk.choices
            and chunk.choices[0].delta
            and chunk.choices[0].delta.content
        ]
        summary = "".join(parts)
        if summary:
            logger.info(
                f"[hold_transfer] Summarized transcription "
                f"({len(transcription)} turns -> {len(summary)} chars)"
            )
        return summary or None
    except Exception as e:
        logger.error(
            f"[hold_transfer] Summarization failed, will fall back to "
            f"raw transcription: {e}",
            exc_info=True,
        )
        return None


async def _wait_for_message(pubsub) -> Optional[Dict[str, Any]]:
    """Read messages until we get a data message (skip subscribe confirmations)."""
    async for message in pubsub.listen():
        if message["type"] == "message":
            return json.loads(message["data"])
    return None
