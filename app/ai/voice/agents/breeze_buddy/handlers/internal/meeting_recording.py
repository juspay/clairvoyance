"""Meeting recording: the start builtin, and what happens when it stops.

Start goes through the LLM (any phrasing); stop is caught by the gate's
capture mode (whole-sentence phrase match). Both doors land in the
MeetingSession; this module is the glue between a live call and that session:

- ``start_meeting_recording``: the builtin the template offers the LLM.
- ``_on_finished``: tells the LLM to say how long it recorded, notes the
  meeting on the lead, and delivers the transcript via on_stop_hooks.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, Optional

from pipecat.frames.frames import LLMMessagesAppendFrame

from app.ai.voice.agents.breeze_buddy.recorder.config import meeting_recording_enabled
from app.ai.voice.agents.breeze_buddy.recorder.delivery import deliver
from app.ai.voice.agents.breeze_buddy.recorder.session import (
    MeetingConfig,
    MeetingSession,
    split_stop_phrases,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.core.logger import logger
from app.database.accessor.breeze_buddy import (
    lead_call_tracker as lead_db,
    meetings as meetings_db,
)
from app.schemas.breeze_buddy.meetings import DeliveryState, Meeting


async def start_meeting_recording(
    context: TemplateContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    """Builtin: open a recording on this call. Idempotent while recording."""
    bot = context.bot
    configurations = context.configurations
    config = getattr(configurations, "meeting_recording", None)
    if config is None or not meeting_recording_enabled(configurations):
        return {"status": "error", "error": "meeting recording is not enabled"}

    existing: Optional[MeetingSession] = getattr(bot, "_meeting_session", None)
    if existing is not None and existing.is_recording:
        return {
            "status": "already_recording",
            "meeting_id": existing.meeting_id,
            "instruction": "A recording is already running; say so briefly.",
        }

    gate = getattr(bot, "speech_gate", None)
    lead = context.lead
    if gate is None or lead is None:
        return {"status": "error", "error": "recording is unavailable on this call"}
    merchant_id = lead.merchant_id or getattr(bot.template, "merchant_id", None)
    if not merchant_id:
        return {"status": "error", "error": "this call has no merchant"}

    meeting_config = await MeetingConfig.load()
    template_phrases = split_stop_phrases(config.stop_phrases) + split_stop_phrases(
        config.stop_phrase
    )
    if template_phrases:
        meeting_config = replace(meeting_config, stop_phrases=template_phrases)
    stt_configuration = getattr(configurations, "stt_configuration", None)
    stt_provider = stt_configuration.provider.value if stt_configuration else "soniox"

    async def on_finished(meeting: Meeting) -> None:
        await _on_finished(context, meeting)

    session = MeetingSession(
        gate=gate,
        config=meeting_config,
        lead_id=lead.id,
        merchant_id=merchant_id,
        reseller_id=lead.reseller_id,
        stt_provider=stt_provider,
        idle_handler=getattr(bot, "_user_idle_callback_handler", None),
        stt_service=getattr(bot, "_stt_service", None),
        emit=getattr(bot, "_emit_rtvi_event", None),
        on_finished=on_finished,
    )
    try:
        meeting = await session.start()
    except Exception as e:
        logger.error(f"[MEETING] start failed on lead {lead.id}: {e}")
        return {"status": "error", "error": "could not start the recording"}

    bot._meeting_session = session
    return {
        "status": "recording_started",
        "meeting_id": meeting.id,
        "instruction": (
            f"Say exactly: '{config.start_confirmation}' Then say nothing more "
            "until you are told the recording has stopped."
        ),
    }


async def _on_finished(context: TemplateContext, meeting: Meeting) -> None:
    """After stop: speak the duration, note the lead, deliver the transcript."""
    duration = meeting.duration_s or 0
    minutes, seconds = divmod(duration, 60)
    spoken = f"{minutes} minutes" if minutes else f"{seconds} seconds"

    task = context.task
    if task is not None:
        try:
            await task.queue_frame(
                LLMMessagesAppendFrame(
                    [
                        {
                            "role": "system",
                            "content": (
                                f"The meeting recording has stopped after {spoken}. "
                                f"Tell the user: 'Recorded your meeting for {spoken}. "
                                "What's next for us to proceed?' and then continue "
                                "as a normal assistant."
                            ),
                        }
                    ],
                    run_llm=True,
                )
            )
        except Exception as e:
            logger.warning(f"[MEETING] could not queue the stop reply: {e}")

    lead = context.lead
    if lead is not None:
        if lead.metaData is None:
            lead.metaData = {}
        ids = list(lead.metaData.get("meeting_ids") or [])
        if meeting.id not in ids:
            ids.append(meeting.id)
        lead.metaData["meeting_ids"] = ids
        try:
            await lead_db.append_metadata_field(lead.id, {"meeting_ids": ids})
        except Exception as e:
            logger.warning(
                f"[MEETING] could not note {meeting.id} on lead {lead.id}: {e}"
            )

    block = getattr(context.configurations, "meeting_recording", None)
    hooks = block.on_stop_hooks if block is not None else []
    http_session = context.aiohttp_session
    if not hooks or http_session is None:
        logger.info(f"[MEETING] {meeting.id} finished; no delivery configured")
        return
    try:
        segments = await meetings_db.get_segments(meeting.id)
        ok, error = await deliver(meeting, segments, hooks, http_session)
        await meetings_db.set_delivery_state(
            meeting.id, DeliveryState.SENT if ok else DeliveryState.FAILED, error
        )
    except Exception as e:
        logger.error(f"[MEETING] delivery of {meeting.id} raised: {e}")
        try:
            await meetings_db.set_delivery_state(
                meeting.id, DeliveryState.FAILED, f"{type(e).__name__}: {e}"[:1000]
            )
        except Exception:
            pass
