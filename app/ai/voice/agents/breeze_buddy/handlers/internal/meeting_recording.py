"""Meeting recording: the start builtin, and what happens when it ends.

Start goes through the LLM (any phrasing); stop is heard by the observer
(whole-sentence phrase match) or by a cap. Both land in the MeetingSession.
This module is the glue between a live call and that session:

- ``start_meeting_recording``: the builtin the template offers the LLM.
- ``on_meeting_finished``: tells the LLM to say how long it recorded, and
  delivers the transcript through the template's on_stop_hooks.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, cast

import aiohttp
from pipecat.frames.frames import LLMMessagesAppendFrame, TTSSpeakFrame

from app.ai.voice.agents.breeze_buddy.recorder.delivery import deliver
from app.ai.voice.agents.breeze_buddy.recorder.session import (
    END_CALL_ENDED,
    MeetingRecord,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.core.logger import logger


async def start_meeting_recording(
    context: TemplateContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    """Builtin: begin recording on this call. Idempotent while recording."""
    bot = context.bot
    session = getattr(bot, "_meeting_session", None)
    block = getattr(context.configurations, "meeting_recording", None)
    if session is None or block is None or not block.enabled:
        return {
            "status": "error",
            "error": "meeting recording is not enabled on this call",
        }
    if session.is_recording:
        return {
            "status": "already_recording",
            "meeting_id": session.meeting_id,
            "instruction": "A recording is already running; say so briefly.",
        }
    try:
        meeting_id = await session.start()
    except Exception as e:
        logger.error(f"[MEETING] start failed on lead {session.lead_id}: {e}")
        return {"status": "error", "error": "could not start the recording"}
    return {
        "status": "recording_started",
        "meeting_id": meeting_id,
        "instruction": block.start_instruction.replace(
            "{start_confirmation}", block.start_confirmation
        ),
    }


def _spoken_duration(seconds: int) -> str:
    """41 → '41 seconds', 60 → '1 minute', 2479 → '41 minutes'."""
    minutes, rest = divmod(max(0, seconds), 60)
    if minutes == 0:
        return f"{rest} second{'s' if rest != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


async def on_meeting_finished(bot: Any, record: MeetingRecord) -> None:
    """After stop: speak the duration (unless the call is over), deliver."""
    block = getattr(getattr(bot, "configurations", None), "meeting_recording", None)
    template_line = (
        block.stop_confirmation
        if block is not None
        else "Recorded your meeting for {duration}."
    )
    reply = template_line.replace("{duration}", _spoken_duration(record.duration_s))

    task = getattr(bot, "task", None)
    call_over = record.end_reason == END_CALL_ENDED or getattr(
        bot, "conversation_ended", False
    )
    if task is not None and not call_over:
        # Speak the line directly and record it in the context as something
        # the assistant already said. A standing system instruction ("tell
        # the user …") made the LLM repeat the sentence on later turns.
        try:
            await task.queue_frames(
                [
                    TTSSpeakFrame(text=reply),
                    LLMMessagesAppendFrame(
                        [{"role": "assistant", "content": reply}], run_llm=False
                    ),
                ]
            )
        except Exception as e:
            logger.warning(f"[MEETING] could not queue the stop reply: {e}")

    hooks = block.on_stop_hooks if block is not None else []
    raw_session = getattr(bot, "aiohttp_session", None)
    if not hooks or raw_session is None:
        logger.info(f"[MEETING] {record.meeting_id} finished; no delivery configured")
        return
    http_session = cast(aiohttp.ClientSession, raw_session)

    async def _deliver() -> None:
        try:
            ok, error = await deliver(record, hooks, http_session)
            if not ok:
                logger.warning(f"[MEETING] {record.meeting_id} not delivered: {error}")
        except Exception as e:
            logger.error(f"[MEETING] delivery of {record.meeting_id} raised: {e}")

    if call_over:
        await _deliver()  # teardown path: the session closes right after us
    else:
        bot._meeting_delivery_task = asyncio.create_task(_deliver())
