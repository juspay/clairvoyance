"""Build the session and its two attachments for a call, when the template
enables meeting recording. Called by the agent before the pipeline is built;
returns None for every other template, so nothing changes for them."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional

from app.ai.voice.agents.breeze_buddy.handlers.internal.meeting_recording import (
    on_meeting_finished,
)
from app.ai.voice.agents.breeze_buddy.recorder.config import meeting_recording_enabled
from app.ai.voice.agents.breeze_buddy.recorder.mute_strategy import (
    MeetingRecordingMuteStrategy,
)
from app.ai.voice.agents.breeze_buddy.recorder.observer import (
    MeetingRecordingObserver,
)
from app.ai.voice.agents.breeze_buddy.recorder.phrases import split_phrases
from app.ai.voice.agents.breeze_buddy.recorder.session import (
    MeetingConfig,
    MeetingRecord,
    MeetingSession,
)
from app.core.logger import logger


@dataclass
class MeetingAttachments:
    session: MeetingSession
    mute_strategy: MeetingRecordingMuteStrategy
    observer: MeetingRecordingObserver


async def build_meeting_recording(bot: Any) -> Optional[MeetingAttachments]:
    """Session + mute strategy + observer for this call, or None."""
    configurations = getattr(bot, "configurations", None)
    if not meeting_recording_enabled(configurations):
        return None
    lead = getattr(bot, "lead", None)
    if lead is None:
        return None
    merchant_id = lead.merchant_id or getattr(bot.template, "merchant_id", None)
    if not merchant_id:
        logger.warning(
            "Meeting recording: lead has no merchant; feature off for this call"
        )
        return None

    block = getattr(configurations, "meeting_recording", None)
    if block is None:
        return None
    config = await MeetingConfig.load()
    template_phrases = split_phrases(block.stop_phrases) + split_phrases(
        block.stop_phrase
    )
    if template_phrases:
        config = replace(config, stop_phrases=template_phrases)

    async def on_finished(record: MeetingRecord) -> None:
        await on_meeting_finished(bot, record)

    session = MeetingSession(
        config=config,
        lead_id=lead.id,
        merchant_id=merchant_id,
        reseller_id=lead.reseller_id,
        emit=getattr(bot, "_emit_rtvi_event", None),
        on_finished=on_finished,
    )
    logger.info(
        f"Meeting recording armed for lead {lead.id} "
        f"(stop phrases: {len(config.stop_phrases)})"
    )
    return MeetingAttachments(
        session=session,
        mute_strategy=MeetingRecordingMuteStrategy(session),
        observer=MeetingRecordingObserver(session),
    )
