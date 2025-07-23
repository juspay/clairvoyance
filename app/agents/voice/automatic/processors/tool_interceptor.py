# tool_interceptor.py

import asyncio
import re
from collections import deque
from typing import Dict, Optional
from app.core.logger import logger
from pipecat.frames.frames import (
    Frame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    TTSSpeakFrame,
    TextFrame,
    FunctionCallCancelFrame,
    UserStartedSpeakingFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIProcessor, RTVIServerMessageFrame

class ToolInterceptor(FrameProcessor):
    """Intercepts tool/function calls for logging, confirmation, and context-aware filtering."""

    def __init__(
        self,
        rtvi: RTVIProcessor,
        llm_service=None,
        name: str = "ToolInterceptor",
        require_confirmation: bool = False,
    ):
        super().__init__(name=name)
        self._rtvi = rtvi
        self._require_confirmation = require_confirmation
        self._pending_confirmations: Dict[str, Dict] = {}
        self._confirmed_calls: set = set()
        self._cancelled_calls: set = set()
        self._blocked_calls: set = set() # This will now track calls that are blocked pending confirmation
        self._waiting_for_confirmation = False # True if currently asking for confirmation for one tool
        self._processing_confirmation = False # True if currently evaluating user's 'yes'/'no'
        self._confirmation_queue: deque[FunctionCallInProgressFrame] = deque()
        self._current_confirmation_tool_call_id: Optional[str] = None

        # Context memory: function_name -> confirmed (True/False)
        self._confirmation_context: Dict[str, bool] = {}

    def _is_context_confirmed(self, function_name: str) -> Optional[bool]:
        """Check if the function_name is already confirmed/denied in context."""
        return self._confirmation_context.get(function_name)

    def _set_context_confirmation(self, function_name: str, confirmed: bool):
        """Set confirmation context for a function_name."""
        self._confirmation_context[function_name] = confirmed

    async def _evaluate_confirmation_response(self, user_text: str) -> bool:
        logger.info(f"[ToolInterceptor] Evaluating user response: '{user_text}'")
        cleaned_text = re.sub(r"[^\w\s]", "", user_text.lower().strip())
        logger.info(f"[ToolInterceptor] Cleaned text: '{cleaned_text}'")
        confirm_keywords = [
            "yes", "yeah", "yep", "okay", "ok", "sure", "proceed", "go ahead",
            "confirm", "do it", "allow"
        ]
        deny_keywords = [
            "no", "nope", "cancel", "stop", "deny", "abort", "never mind",
            "not now", "dont", "don't", "refuse"
        ]
        if cleaned_text in confirm_keywords:
            logger.info(f"[ToolInterceptor] EXACT confirmation match: '{cleaned_text}'")
            return True
        elif cleaned_text in deny_keywords:
            logger.info(f"[ToolInterceptor] EXACT denial match: '{cleaned_text}'")
            return False
        for confirm_word in confirm_keywords:
            if (
                f" {confirm_word} " in f" {cleaned_text} "
                or cleaned_text.startswith(f"{confirm_word} ")
                or cleaned_text.endswith(f" {confirm_word}")
            ):
                logger.info(f"[ToolInterceptor] Found confirmation word '{confirm_word}' in: '{cleaned_text}'")
                return True
        for deny_word in deny_keywords:
            if (
                f" {deny_word} " in f" {cleaned_text} "
                or cleaned_text.startswith(f"{deny_word} ")
                or cleaned_text.endswith(f" {deny_word}")
            ):
                logger.info(f"[ToolInterceptor] Found denial word '{deny_word}' in: '{cleaned_text}'")
                return False
        logger.info(f"[ToolInterceptor] NO CLEAR MATCH found, defaulting to DENY: '{user_text}'")
        return False

    async def _wait_for_confirmation(self, tool_call_id: str, function_name: str) -> bool:
        self._waiting_for_confirmation = True
        confirmation_data = {
            "function_name": function_name,
            "confirmed": None,
            "event": asyncio.Event(),
        }
        self._pending_confirmations[tool_call_id] = confirmation_data
        logger.info(f"[ToolInterceptor] Waiting for confirmation for {function_name} (ID: {tool_call_id}) - no timeout")
        try:
            await confirmation_data["event"].wait()
            result = confirmation_data.get("confirmed", False)
            logger.info(f"[ToolInterceptor] Confirmation result for {function_name}: {result}")
            # Store in context ONLY if confirmed, so denied tools are re-asked
            if result:
                self._set_context_confirmation(function_name, result)
            return result
        finally:
            self._pending_confirmations.pop(tool_call_id, None)
            self._waiting_for_confirmation = False
            self._current_confirmation_tool_call_id = None
            logger.info(f"[ToolInterceptor] Finished waiting for confirmation for {function_name}")
            # After a confirmation is resolved, try to process the next queued call
            asyncio.create_task(self._process_next_queued_call())

    async def _handle_confirmation_response(self, user_text: str):
        if not self._pending_confirmations:
            logger.warning("[ToolInterceptor] _handle_confirmation_response called but no pending confirmations.")
            return

        # Only process the latest pending confirmation
        tool_call_id = list(self._pending_confirmations.keys())[-1]
        confirmation_data = self._pending_confirmations[tool_call_id]

        if confirmation_data["event"].is_set():
            logger.info(f"[ToolInterceptor] Confirmation for {tool_call_id} already processed. Ignoring new response.")
            return

        self._processing_confirmation = True
        try:
            logger.info(f"[ToolInterceptor] Processing confirmation response: '{user_text}' for tool_call_id: {tool_call_id}")
            is_confirmed = await self._evaluate_confirmation_response(user_text)
            confirmation_data["confirmed"] = is_confirmed
            confirmation_data["event"].set()
            # Store in context ONLY if confirmed, so denied tools are re-asked
            if is_confirmed:
                self._set_context_confirmation(confirmation_data["function_name"], is_confirmed)
            logger.info(f"[ToolInterceptor] Confirmation processed for {tool_call_id}: {'CONFIRMED' if is_confirmed else 'DENIED'}")
        except Exception as e:
            logger.error(f"[ToolInterceptor] Error processing confirmation for {tool_call_id}: {e}", exc_info=True)
        finally:
            self._processing_confirmation = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # Handle function call cancellation frames
        if isinstance(frame, FunctionCallCancelFrame):
            logger.info(f"[ToolInterceptor] Function call cancel frame received: {frame.tool_call_id}")
            if frame.tool_call_id in self._cancelled_calls or frame.tool_call_id in self._blocked_calls:
                logger.info(f"[ToolInterceptor] Cancellation already handled for {frame.tool_call_id}, not passing downstream")
                self._cancelled_calls.discard(frame.tool_call_id)
                self._blocked_calls.discard(frame.tool_call_id)
                return
            await self.push_frame(frame, direction)
            return

        # Handle user input for confirmations - check multiple frame types
        if self._require_confirmation and self._waiting_for_confirmation:
            text_content = None
            frame_type = type(frame).__name__
            if isinstance(frame, TranscriptionFrame):
                text_content = frame.text
                logger.info(f"[ToolInterceptor] Got TranscriptionFrame: '{text_content}'")
            elif isinstance(frame, TextFrame):
                text_content = frame.text
                logger.info(f"[ToolInterceptor] Got TextFrame: '{text_content}'")
            elif hasattr(frame, "text") and frame.text:
                text_content = frame.text
                logger.info(f"[ToolInterceptor] Got frame with text attribute ({frame_type}): '{text_content}'")
            elif hasattr(frame, "content") and frame.content:
                text_content = frame.content
                logger.info(f"[ToolInterceptor] Got frame with content attribute ({frame_type}): '{text_content}'")
            elif hasattr(frame, "transcript") and frame.transcript:
                text_content = frame.transcript
                logger.info(f"[ToolInterceptor] Got frame with transcript attribute ({frame_type}): '{text_content}'")
            elif hasattr(frame, "message") and frame.message:
                text_content = frame.message
                logger.info(f"[ToolInterceptor] Got frame with message attribute ({frame_type}): '{text_content}'")
            if text_content and text_content.strip():
                logger.info(f"[ToolInterceptor] Processing confirmation text during wait (frame: {frame_type}): '{text_content}'")
                await self._handle_confirmation_response(text_content)
                await self.push_frame(frame, direction)
                return

        if isinstance(frame, FunctionCallInProgressFrame):
            function_name = frame.function_name
            logger.info(f"[ToolInterceptor] Function call intercepted - START: {function_name}")
            logger.info(f"[ToolInterceptor] Tool Call ID: {frame.tool_call_id}")
            logger.info(f"[ToolInterceptor] Arguments: {frame.arguments}")

            if not self._require_confirmation:
                logger.info(f"[ToolInterceptor] Status: ALLOWING (confirmations disabled)")
                await self.push_frame(frame, direction)
                return

            context_confirmed = self._is_context_confirmed(function_name)
            if context_confirmed: # Only proceed if explicitly confirmed
                logger.info(f"[ToolInterceptor] Context: already confirmed {function_name}, proceeding without asking.")
                await self.push_frame(frame, direction)
                return
            # If not explicitly confirmed (either never asked or previously denied), proceed to confirmation flow

            # If a confirmation is already in progress, queue this new call
            if self._waiting_for_confirmation:
                logger.info(f"[ToolInterceptor] Queuing new function call {function_name} - confirmation already in progress.")
                self._confirmation_queue.append(frame)
                self._blocked_calls.add(frame.tool_call_id) # Mark as blocked until confirmed
                return

            # If not waiting, start confirmation for this call
            await self._initiate_confirmation_flow(frame, direction)
            return

        elif isinstance(frame, FunctionCallResultFrame):
            formatted_function_name = frame.function_name.replace("_", " ")
            if not self._require_confirmation:
                logger.info(f"[ToolInterceptor] Function call RESULT: {formatted_function_name} (confirmations disabled)")
                await self.push_frame(frame, direction)
                return
            if frame.tool_call_id in self._confirmed_calls:
                logger.info(f"[ToolInterceptor] Function call RESULT for confirmed call: {formatted_function_name}")
                self._confirmed_calls.remove(frame.tool_call_id)
                await self.push_frame(frame, direction)
            else:
                logger.info(f"[ToolInterceptor] Function call RESULT for unconfirmed call - blocking: {formatted_function_name}")
                return
        else:
            await self.push_frame(frame, direction)

    async def _initiate_confirmation_flow(self, frame: FunctionCallInProgressFrame, direction: FrameDirection):
        function_name = frame.function_name
        tool_call_id = frame.tool_call_id

        logger.info(f"[ToolInterceptor] Status: ASKING FOR CONFIRMATION for {function_name}")
        self._waiting_for_confirmation = True
        self._current_confirmation_tool_call_id = tool_call_id
        self._blocked_calls.add(tool_call_id)

        formatted_function_name = function_name.replace("_", " ")
        confirmation_message = (
            f"I need permission to {formatted_function_name}. Please say 'yes' to allow or 'no' to cancel."
        )

        try:
            import time
            rtvi_message_data = {
                "type": "tool-confirmation-request",
                "payload": {
                    "toolCallId": tool_call_id,
                    "functionName": function_name,
                    "message": confirmation_message,
                    "timestamp": int(time.time() * 1000),
                },
            }
            rtvi_message = RTVIServerMessageFrame(data=rtvi_message_data)
            await self._rtvi.push_frame(rtvi_message)
            logger.info(f"[ToolInterceptor] RTVI message sent for {function_name}")
        except Exception as e:
            logger.error(f"[ToolInterceptor] Error sending RTVI message: {e}", exc_info=True)

        try:
            confirmation_frame = TTSSpeakFrame(confirmation_message)
            await self.push_frame(confirmation_frame, FrameDirection.DOWNSTREAM)
            logger.info(f"[ToolInterceptor] TTS confirmation request sent for {function_name}")
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"[ToolInterceptor] Error sending TTS frame: {e}", exc_info=True)

        is_confirmed = await self._wait_for_confirmation(tool_call_id, function_name)
        self._blocked_calls.discard(tool_call_id)

        if is_confirmed:
            logger.info(f"[ToolInterceptor] User CONFIRMED - proceeding with {function_name}")
            self._confirmed_calls.add(tool_call_id)
            proceed_message = f"Proceeding with {formatted_function_name}."
            proceed_frame = TTSSpeakFrame(proceed_message)
            await self.push_frame(proceed_frame, FrameDirection.DOWNSTREAM)
            await asyncio.sleep(0.1)
            logger.info(f"[ToolInterceptor] Passing original frame downstream: {tool_call_id}")
            await self.push_frame(frame, direction)
        else:
            logger.info(f"[ToolInterceptor] User DENIED - canceling {function_name}")
            cancel_message = f"I understand. Canceling {formatted_function_name}."
            cancel_frame = TTSSpeakFrame(cancel_message)
            await self.push_frame(cancel_frame, FrameDirection.DOWNSTREAM)
            try:
                cancel_function_frame = FunctionCallCancelFrame(
                    tool_call_id=tool_call_id,
                    function_name=function_name
                )
                self._cancelled_calls.add(tool_call_id)
                await self.push_frame(cancel_function_frame, direction)
                await asyncio.sleep(0.1) # Small delay for propagation
            except Exception as e:
                logger.error(f"[ToolInterceptor] Error creating cancel frame: {e}", exc_info=True)

    async def _process_next_queued_call(self):
        if not self._waiting_for_confirmation and self._confirmation_queue:
            next_frame = self._confirmation_queue.popleft()
            logger.info(f"[ToolInterceptor] Processing next queued tool call: {next_frame.function_name}")
            # Re-initiate the confirmation flow for the next queued call
            await self._initiate_confirmation_flow(next_frame, FrameDirection.DOWNSTREAM) # Assuming downstream for queued calls
