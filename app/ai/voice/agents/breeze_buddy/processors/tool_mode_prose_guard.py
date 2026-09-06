"""ToolModeProseGuardProcessor — tool_based mode's model-prose firewall.

tool_based templates move ALL speech into template-authored ``say`` blocks
rendered by function handlers (``TTSSpeakFrame``). The model's contract is
"one tool call per turn, never prose". This processor is the hard guarantee
sitting between the LLM and TTS: any ``LLMTextFrame`` the model emits anyway
(preamble before a tool call, or a rare no-tool turn when the provider
doesn't support ``tool_choice="required"``) is dropped before it can reach
TTS *and* the downstream assistant aggregator — so stray prose neither
speaks nor pollutes the LLM's own context.

``TTSSpeakFrame`` (the tool speech path) is a ``DataFrame``, not a
``TextFrame``, so it passes through untouched. Everything else — response
boundary frames, metrics, transcriptions — flows unchanged.

Placement: immediately after the LLM service, before TTS
(``... user_aggregator → llm → [guard] → tts → ... → assistant_aggregator``).
"""

from __future__ import annotations

from pipecat.frames.frames import (
    Frame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.core.logger import logger


class ToolModeProseGuardProcessor(FrameProcessor):
    """Drops LLM-authored text frames in tool_based mode; passes all else."""

    def __init__(self, *, name: str = "ToolModeProseGuardProcessor") -> None:
        super().__init__(name=name)
        self.dropped_prose_count = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMTextFrame):
            self.dropped_prose_count += 1
            # Loud on purpose: in a healthy tool_based call this never fires;
            # when it does, the customer heard nothing for this turn and the
            # template's rules / tool_choice setting need attention.
            logger.warning(
                f"[tool_based] dropped model prose "
                f"(#{self.dropped_prose_count}): {frame.text[:200]!r}"
            )
            return

        await self.push_frame(frame, direction)
