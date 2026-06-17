"""VoiceUiStreamProcessor — generative UI over the voice RTVI channel.

Mirrors chat's ``<ui_stream>`` pipeline (``chat/ui_stream.py``) but emits
validated UI ops over RTVI instead of SSE, and strips the markers from the
assistant text *before* it reaches TTS (so the bot never speaks the JSON) and
before the downstream assistant context aggregator (so the LLM context and the
``end_conversation`` transcript carry clean prose — exactly mirroring chat's
``strip_ui_stream_markers``).

Placed between the LLM service and TTS in :func:`build_pipeline`, gated on a
per-template ``ui_catalog`` opt-in (see ``docs/widget/VOICE_AS_CHAT.md`` A1).
The op heal/parse/validate/repeat/root-anchor/allowlist logic is reused verbatim
from ``chat/ui_stream.py`` (``process_op_line``) — this processor only adapts
its ``SSEEvent`` output into ``_emit_rtvi_event("ui-op", {"op": ...})``.

Daily (standard agent-mode STT/TTS) only. Widget voice now runs **stream**
mode through the chat brain (``chat/voice_bridge.py``), where the chat
``ChatAgent`` already emits + persists ``ui_op`` events — so this processor is
NOT wired for widget voice; it remains for any non-widget daily agent-mode
template that opts into ``ui_catalog``. Telephony has no RTVI processor and
realtime speech-to-speech has no separate TTS stage to intercept (both out of
scope).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional, Set

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.ai.voice.agents.breeze_buddy.chat.ui_healer import (
    HealerContext,
    make_healer_fn,
)
from app.ai.voice.agents.breeze_buddy.chat.ui_stream import (
    TextOut,
    UiStreamExtractor,
    process_op_line,
)
from app.core.logger import logger

# Emit signature matches Agent._emit_rtvi_event(event_type, payload).
RtviEmit = Callable[[str, Optional[Dict[str, Any]]], Awaitable[None]]


def coerce_ui_action_text(
    data: Optional[Dict[str, Any]], max_chars: int
) -> Optional[str]:
    """Validate + normalize a client ``ui-action`` message into the user-turn
    text to inject. Returns ``None`` when the ``msg`` field is missing, not a
    string, or blank. Trims whitespace and caps length (mirrors the tts-speak
    truncation contract). See docs/widget/VOICE_AS_CHAT.md (A2)."""
    text = (data or {}).get("msg", "")
    if not isinstance(text, str) or not text.strip():
        return None
    return text.strip()[:max_chars]


class VoiceUiStreamProcessor(FrameProcessor):
    """Tap streamed LLM text, route ``<ui_stream>`` ops to RTVI, and forward
    only marker-stripped prose downstream to TTS + the assistant aggregator."""

    def __init__(
        self,
        *,
        emit: RtviEmit,
        allowlist: Set[str],
        name: str = "VoiceUiStreamProcessor",
    ) -> None:
        super().__init__(name=name)
        self._emit = emit
        self._allowlist = allowlist
        # Per-assistant-response state, reset on LLMFullResponseStartFrame. A
        # voice call is one long session (chat gets a fresh ChatAgent per
        # turn), so resetting per response keeps the id registry from growing
        # unbounded over a long call (VOICE_AS_CHAT.md A1.3).
        self._extractor = UiStreamExtractor()
        self._known_ids: Set[str] = set()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._extractor = UiStreamExtractor()
            self._known_ids = set()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMTextFrame):
            await self._handle_text(frame.text, direction)
            return

        if isinstance(frame, LLMFullResponseEndFrame):
            # Drain residual prose held in the extractor's carry buffer before
            # forwarding the end-of-response boundary, so trailing prose reaches
            # TTS ahead of the boundary frame.
            for item in self._extractor.flush():
                if isinstance(item, TextOut) and item.value:
                    await self.push_frame(LLMTextFrame(text=item.value), direction)
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

    async def _handle_text(self, text: str, direction: FrameDirection) -> None:
        if not text:
            return
        # Healer mutates ctx.known_ids (dedupe); process_op_line mutates the
        # same set (root-anchor + add/remove tracking) — they MUST share one
        # set, exactly as chat wires it (chat/agent.py _cycle_loop).
        healer = make_healer_fn(
            HealerContext(session_data={}, known_ids=self._known_ids)
        )
        for item in self._extractor.feed(text):
            if isinstance(item, TextOut):
                # Prose (markers stripped) → forward as an LLMTextFrame so TTS
                # speaks it AND the downstream assistant aggregator records it
                # (it keys on LLMTextFrame, not plain TextFrame).
                if item.value:
                    await self.push_frame(LLMTextFrame(text=item.value), direction)
                continue
            # JsonlOpLine → chat-identical heal/parse/validate → RTVI ui-op.
            for ev in process_op_line(
                item.raw,
                session_state={},
                healer=healer,
                known_ids=self._known_ids,
                allowlist=self._allowlist,
            ):
                if ev.event == "ui_op":
                    op = ev.data.get("op") if isinstance(ev.data, dict) else None
                    if isinstance(op, dict):
                        await self._emit("ui-op", {"op": op})
                elif ev.event == "ui_op_dropped":
                    logger.debug(f"[voice-ui] op dropped: {ev.data}")
