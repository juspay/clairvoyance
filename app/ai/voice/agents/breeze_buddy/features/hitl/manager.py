"""HITL Manager for Breeze Buddy - handles voice-native confirmation requests."""

import asyncio
import uuid
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStoppedSpeakingFrame,
)

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.interruption import (
    _apply_interruption_config,
    _get_user_aggregator,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    HITLConfig,
    InterruptionConfig,
    InterruptionMode,
)
from app.core.config.static import BREEZE_BUDDY_HITL_DEFAULT_TIMEOUT
from app.core.logger import logger


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _matches_response(text: str, keywords: List[str]) -> bool:
    """Match a single text chunk against keywords."""
    text = text.lower().strip()

    # Fast substring match
    for keyword in keywords:
        if keyword.lower() in text:
            return True

    # Fuzzy match (safer thresholds)
    for keyword in keywords:
        kw = keyword.lower()
        if abs(len(text) - len(kw)) <= 2:
            threshold = 0.9 if len(kw) <= 3 else 0.8
            if _similarity(text, kw) >= threshold:
                return True

    return False


@dataclass
class PendingConfirmation:
    confirmation_id: str
    config: HITLConfig
    function_name: str
    arguments: Dict[str, Any]
    event: asyncio.Event = field(default_factory=asyncio.Event)
    response: Optional[Dict[str, Any]] = None
    retry_count: int = 0
    max_retries: int = 3
    ask_again: bool = False


class BreezeBuddyHITLManager:
    def __init__(self):
        self._pending: Dict[str, PendingConfirmation] = {}
        self._active_confirmation_id: Optional[str] = None

    def is_confirmation_active(self) -> bool:
        return self._active_confirmation_id is not None

    async def consume_transcription_if_hitl(self, text: str) -> bool:
        """
        Returns True if text is consumed by HITL (and should NOT go to LLM).
        """
        confirmation_id = self._active_confirmation_id
        if not confirmation_id:
            return False

        pending = self._pending.get(confirmation_id)
        if not pending:
            return False

        text = text.strip()
        if not text:
            return True

        logger.info(f"HITL: Heard '{text}'")

        if _matches_response(text, pending.config.accepted_responses):
            pending.response = {"approved": True, "reason": "user_approved"}
            pending.event.set()
            return True

        if _matches_response(text, pending.config.rejected_responses):
            pending.response = {"approved": False, "reason": "user_rejected"}
            pending.event.set()
            return True

        # Unmatched response - increment retry
        pending.retry_count += 1
        if pending.retry_count >= pending.max_retries:
            pending.response = {"approved": False, "reason": "max_retries_exceeded"}
            pending.event.set()
            logger.warning(f"HITL: Max retries exceeded for {pending.function_name}")
            return True

        # Signal to re-prompt and continue listening
        pending.ask_again = True
        pending.event.set()
        return True

    async def request_confirmation(
        self,
        context: TemplateContext,
        config: HITLConfig,
        function_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        confirmation_id = str(uuid.uuid4())

        pending = PendingConfirmation(
            confirmation_id=confirmation_id,
            config=config,
            function_name=function_name,
            arguments=arguments,
        )
        self._pending[confirmation_id] = pending
        self._active_confirmation_id = confirmation_id

        timeout = float(config.timeout_seconds or BREEZE_BUDDY_HITL_DEFAULT_TIMEOUT)
        logger.info(
            f"HITL: Requesting confirmation for {function_name}, timeout={timeout}s"
        )

        try:
            return await self._voice_confirmation(context, pending)
        except asyncio.TimeoutError:
            logger.warning(f"HITL timeout for {function_name}")
            return {"approved": False, "reason": "timeout"}
        finally:
            if self._active_confirmation_id == confirmation_id:
                self._active_confirmation_id = None
            self._cleanup(confirmation_id)

    async def _voice_confirmation(
        self,
        context: TemplateContext,
        pending: PendingConfirmation,
    ) -> Dict[str, Any]:
        task = context.task
        config = pending.config

        if not task:
            logger.error("No task available for HITL confirmation")
            return {"approved": False, "reason": "no_task"}

        message = config.confirmation_message or self._default_message(
            pending.function_name, pending.arguments
        )

        timeout = float(config.timeout_seconds or BREEZE_BUDDY_HITL_DEFAULT_TIMEOUT)

        logger.info(
            f"HITL: Starting confirmation for {pending.function_name}, timeout={timeout}s"
        )

        task.add_reached_upstream_filter((TranscriptionFrame, UserStoppedSpeakingFrame))

        user_aggregator = _get_user_aggregator(context.bot)
        original_config = None

        if user_aggregator:
            original_config = getattr(user_aggregator, "interruption_config", None)
            disable_config = InterruptionConfig(mode=InterruptionMode.DISABLED_DISCARD)

            await _apply_interruption_config(
                user_aggregator,
                disable_config,
                has_vad=context.bot.vad_analyzer is not None,
                call_sid=context.call_sid or "unknown",
                label="hitl_confirmation",
                bot=context.bot,
                user_speech_timeout=0.0,
            )
            logger.info(
                f"HITL: Interruptions disabled (user_speech_timeout=0.0) for {timeout}s confirmation window"
            )

        # Two-phase TTS wait: BotStarted → BotStopped for THIS confirmation prompt
        tts_complete = asyncio.Event()
        bot_started = False

        async def on_bot_tts_window(task_obj, frame):
            nonlocal bot_started
            if tts_complete.is_set():
                return  # This handler's job is done; avoid stale reactions
            if isinstance(frame, BotStartedSpeakingFrame):
                bot_started = True
            elif isinstance(frame, BotStoppedSpeakingFrame) and bot_started:
                tts_complete.set()

        # Ensure we receive BotStartedSpeakingFrame too
        task.add_reached_downstream_filter((BotStartedSpeakingFrame,))
        task.add_event_handler("on_frame_reached_downstream", on_bot_tts_window)

        try:
            await task.queue_frame(TTSSpeakFrame(text=message))

            try:
                await asyncio.wait_for(tts_complete.wait(), timeout=10)
                logger.info("HITL: Confirmation prompt finished speaking")
            except asyncio.TimeoutError:
                logger.warning("HITL: TTS timeout")

            logger.info("HITL: Listening for response...")

            while pending.retry_count < pending.max_retries:
                try:
                    pending.event.clear()
                    await asyncio.wait_for(pending.event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning("HITL: Response timeout")
                    pending.response = {"approved": False, "reason": "timeout"}
                    break

                # Check if we need to ask again
                if pending.ask_again:
                    pending.ask_again = False
                    retry_message = (
                        f"I didn't understand. {message} Please say yes or no."
                    )
                    tts_complete.clear()
                    bot_started = False
                    await task.queue_frame(TTSSpeakFrame(text=retry_message))
                    try:
                        await asyncio.wait_for(tts_complete.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        logger.warning("HITL: Retry TTS timeout")
                    logger.info("HITL: Listening for retry response...")
                    continue

                # Response matched (approved or rejected)
                break

            result = pending.response or {"approved": False, "reason": "no_response"}
            logger.info(f"HITL: Confirmation result={result}")
            return result

        finally:
            # Critical: Restore interruptions AFTER HITL response is complete
            # and has been delivered to LLM state machine
            if user_aggregator and original_config:
                logger.info(
                    f"HITL: Restoring interruptions after confirmation "
                    f"(result={pending.response})"
                )
                await _apply_interruption_config(
                    user_aggregator,
                    original_config,
                    has_vad=context.bot.vad_analyzer is not None,
                    call_sid=context.call_sid or "unknown",
                    label="hitl_restore",
                    bot=context.bot,
                )
                logger.info("HITL: Interruptions restored")

    def _cleanup(self, confirmation_id: str):
        self._pending.pop(confirmation_id, None)

    def _default_message(self, function_name: str, arguments: Dict) -> str:
        action = function_name.replace("_", " ")
        return f"Should I {action}? Please say yes or no."


_hitl_manager: Optional[BreezeBuddyHITLManager] = None


def get_hitl_manager() -> BreezeBuddyHITLManager:
    global _hitl_manager
    if _hitl_manager is None:
        _hitl_manager = BreezeBuddyHITLManager()
    return _hitl_manager
