"""
Interruption Context Processor

Detects when the user speaks while the bot is still talking (TTS active) and
injects a system-level context note into the LLM context BEFORE the user's
transcription is processed. This gives the LLM awareness that its previous
response was interrupted and the user may not have heard the full message.

## Why this exists

In Pipecat, the LLMAssistantAggregator stores the FULL LLM-generated text in
the conversation context — even if the TTS was interrupted mid-sentence and the
user never heard the complete response. This creates a dangerous illusion:

    Context shows:  Assistant: "Your order is for 1 bracelet, 648 rupees,
                     address 136 XYZ Street, pincode 400001.
                     Is that all correct so I can confirm your order?"
                    User: "Yes, ma'am"

    Reality:        Bot spoke: "Your order is for 1 bracelet, 648 rupees,
                     to this address: 136—"
                    User interrupted: "Yes, ma'am" (acknowledging, NOT confirming)

Without this processor, the LLM sees the user saying "yes" after a confirmation
question and calls confirm_order(). With this processor, the LLM receives a
context note explaining the interruption, enabling it to re-read details or
re-ask the question instead.

## Pipeline position

Must be AFTER TranscriptionGateProcessor (so keyword-filtered transcriptions
don't trigger false interruption notes) and BEFORE ResponseStateGate:

    transport.input()
    → stt
    → TranscriptionGateProcessor
    → InterruptionContextProcessor   ← here
    → ResponseStateGate
    → user_aggregator
    → llm
    ...

## Frame flow on interruption

1. TranscriptionFrame arrives while TTS is active
2. Processor pushes LLMMessagesAppendFrame (system note, run_llm=False)
3. Processor pushes original TranscriptionFrame
4. LLMMessagesAppendFrame passes through ResponseStateGate (not buffered)
5. TranscriptionFrame triggers interruption in ResponseStateGate (buffered)
6. After interruption completes, TranscriptionFrame is flushed
7. LLM context now has: [..., assistant_msg, system_note, user_msg]
"""

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMMessagesAppendFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.core.logger import logger

# System note injected into context when an interruption is detected.
# Designed to prevent the LLM from treating mid-speech acknowledgments
# (e.g., "yes ma'am", "haan ji") as order confirmations.
INTERRUPTION_CONTEXT_NOTE = (
    "[INTERRUPTION DETECTED] Your previous response was interrupted by the user "
    "before you finished speaking. The user may NOT have heard your complete message. "
    "If your response included reading order details followed by a confirmation question, "
    "the user likely only heard a partial message and their reply is an acknowledgment of "
    "what they heard so far — NOT an answer to a question they never heard. "
    "You MUST re-read any remaining order details and explicitly ask the confirmation "
    "question again before treating any response as a confirmation. "
    "Do NOT call confirm_order based on a response that followed an interrupted message."
)


class InterruptionContextProcessor(FrameProcessor):
    """Detects TTS interruptions and injects context notes for the LLM.

    When the user speaks while the bot is actively speaking (TTS), this
    processor injects a system message into the LLM context to inform
    the LLM that its previous response was cut short. This prevents
    the LLM from treating mid-speech acknowledgments as confirmations.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._tts_active: bool = False
        self._llm_responded: bool = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # Track TTS state
        if isinstance(frame, BotStartedSpeakingFrame):
            self._tts_active = True

        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._tts_active = False

        elif isinstance(frame, LLMFullResponseEndFrame):
            # LLM has finished generating — TTS may still be speaking
            self._llm_responded = True

        elif isinstance(frame, TranscriptionFrame):
            # User spoke — check if bot was still speaking
            if self._tts_active and self._llm_responded:
                logger.info(
                    "InterruptionContext: User spoke while TTS active — "
                    "injecting interruption context note"
                )
                # Push the system note FIRST (will be added to context before
                # the user's transcription). run_llm=False means it only
                # appends to context without triggering a new LLM inference.
                await self.push_frame(
                    LLMMessagesAppendFrame(
                        messages=[
                            {
                                "role": "system",
                                "content": INTERRUPTION_CONTEXT_NOTE,
                            }
                        ],
                        run_llm=False,
                    ),
                    direction,
                )
                # Reset flag — note injected for this interruption
                self._llm_responded = False

            # Push the transcription frame downstream (after the note, if any)
            await self.push_frame(frame, direction)
            return

        # Pass all other frames through
        await self.push_frame(frame, direction)
