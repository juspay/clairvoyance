"""
Interruption Context Processor — template-agnostic interruption awareness for LLMs.

Detects when the user speaks while the bot is still talking (TTS active) and
injects a system-level context note into the LLM context BEFORE the user's
transcription is processed. This gives the LLM accurate awareness that its
previous response was interrupted and the user did NOT hear the full message.

## Why this exists

Both Cartesia and ElevenLabs TTS services in Pipecat have word-level timestamp
support (`supports_word_timestamps=True`, `push_text_frames=False`). This means
that on interruption, the assistant aggregator stores ONLY the words that were
actually spoken — not the full LLM-generated text.

However, the LLM still doesn't explicitly KNOW it was interrupted. It just sees
a shorter assistant message followed by a user response. Without the interruption
note, the LLM has no way to distinguish between:

    (a) A complete response that happened to be short:
        Assistant: "Your order total is 648 rupees. Shall I confirm?"
        User: "Yes"

    (b) An interrupted response where the user acknowledged mid-speech:
        Assistant: "Your order total is 648 rupees, delivered to address 136,"
        User: "Yes, ma'am"  (just acknowledging, not confirming)

With this processor, case (b) gets an explicit interruption note, enabling the
LLM to continue from where it was cut off rather than treating the acknowledgment
as a final answer.

## Pipeline position

Must be AFTER TranscriptionGateProcessor (so keyword-filtered transcriptions
don't trigger false interruption notes) and BEFORE ResponseStateGate:

    transport.input()
    → stt
    → TranscriptionGateProcessor   (drops keyword matches while bot is active)
    → InterruptionContextProcessor  ← here
    → ResponseStateGate
    → user_aggregator
    → llm → tts → transport.output → assistant_aggregator

## How it detects interruptions

BotStartedSpeakingFrame and BotStoppedSpeakingFrame are SystemFrames emitted
by the output transport. They flow UPSTREAM through the pipeline, reaching this
processor even though it sits before the LLM/TTS in the downstream direction.

When a TranscriptionFrame (downstream from STT) arrives while _tts_active is
True, the user spoke during bot speech — i.e., an interruption.

## Frame flow on interruption

1. TranscriptionFrame arrives (downstream) while TTS is active
2. Processor pushes LLMMessagesAppendFrame (system note, run_llm=False) downstream
3. Processor pushes original TranscriptionFrame downstream
4. LLMMessagesAppendFrame passes through ResponseStateGate (not a transcription)
5. TranscriptionFrame triggers interruption in ResponseStateGate (buffered)
6. TTS stops, word timestamps stop, assistant aggregator pushes partial text to context
7. ResponseStateGate flushes buffered TranscriptionFrame
8. LLM context now has: [..., partial_assistant_msg, system_note, user_msg]
"""

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    LLMMessagesAppendFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.core.logger import logger

# Generic interruption context note — works for any template/flow, not just
# order confirmation. Instructs the LLM to continue from where it left off
# rather than treating the user's response as an answer to an unfinished prompt.
INTERRUPTION_CONTEXT_NOTE = (
    "[INTERRUPTION DETECTED] Your previous response was interrupted by the user "
    "before you finished speaking. The user did NOT hear your complete message. "
    "Your context shows only the portion that was actually spoken before the "
    "interruption — the user's reply is to THAT partial message, not to anything "
    "you intended to say afterward. "
    "IMPORTANT: If you were in the middle of reading information, listing details, "
    "or asking a question, the user's short reply (e.g., 'yes', 'ok', 'right', "
    "'haan ji') is almost certainly a conversational acknowledgment — NOT an answer "
    "to a question they never heard. You MUST: "
    "(1) Acknowledge their response briefly, "
    "(2) Continue from where you were interrupted, finishing any remaining information, "
    "(3) Then ask your question or prompt clearly before expecting a meaningful answer. "
    "Do NOT take any decisive action (confirm, cancel, update, transition) based on "
    "a response that followed an interrupted message."
)


class InterruptionContextProcessor(FrameProcessor):
    """Detects TTS interruptions and injects context notes for the LLM.

    Template-agnostic: works for any conversation flow (order confirmation,
    support, surveys, etc.). When the user speaks while the bot is actively
    speaking (TTS), this processor injects a system message into the LLM
    context explaining that the previous response was cut short.

    This prevents the LLM from:
    - Treating mid-speech acknowledgments as confirmations
    - Treating partial responses as complete answers
    - Taking decisive action based on a response to an interrupted message
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Tracks whether the bot is currently speaking (TTS outputting audio).
        # Set by BotStartedSpeakingFrame (upstream from transport.output),
        # cleared by BotStoppedSpeakingFrame.
        self._tts_active: bool = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # Track TTS state from upstream SystemFrames.
        # BotStartedSpeakingFrame/BotStoppedSpeakingFrame flow upstream from
        # transport.output through the entire pipeline.
        if isinstance(frame, BotStartedSpeakingFrame):
            self._tts_active = True
            await self.push_frame(frame, direction)

        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._tts_active = False
            await self.push_frame(frame, direction)

        elif isinstance(frame, TranscriptionFrame):
            # User spoke. If TTS is active, this is an interruption.
            # The TranscriptionGateProcessor upstream already filtered keyword
            # matches (e.g., "yes", "ok", "hmm"), so anything reaching here
            # passed through the keyword filter — it's a non-trivial utterance
            # during bot speech.
            if self._tts_active:
                logger.info(
                    f"InterruptionContext: User spoke while TTS active "
                    f"(transcript: '{frame.text[:50]}') — injecting interruption note"
                )
                # Push the system note FIRST. It flows downstream through
                # ResponseStateGate (which won't buffer it since it's not a
                # TranscriptionFrame) and reaches the LLM context before the
                # user's transcription.
                # run_llm=False: only append to context, don't trigger inference.
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

            # Always push the transcription downstream (with or without note).
            await self.push_frame(frame, direction)

        else:
            # Pass all other frames through unchanged.
            await self.push_frame(frame, direction)
