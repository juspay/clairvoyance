"""Input and response gates for custom voice guardrails.

The input processor runs the platform-owned deterministic check synchronously
from the finalized ``LLMContextFrame`` and awaits the semantic observer check
before releasing safe turns. Blocked turns never reach KB retrieval or the main
LLM.
The response gate is downstream of the main LLM and upstream of TTS. When
output guarding is enabled it uses the same sentence aggregator as Pipecat TTS,
evaluates one completed sentence at a time, and forwards approved sentences as
``AggregatedTextFrame`` objects so TTS does not aggregate them a second time.
"""

from __future__ import annotations

import asyncio

from pipecat.frames.frames import (
    AggregatedTextFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.text.base_text_aggregator import AggregationType
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator

from app.ai.voice.agents.breeze_buddy.guardrails.evaluator import (
    GuardrailCoordinator,
    GuardrailEnforcementError,
)
from app.core.logger import logger


class InputGuardrailProcessor(FrameProcessor):
    """Release user context only after the input guardrail allows the turn."""

    def __init__(self, coordinator: GuardrailCoordinator, **kwargs):
        super().__init__(**kwargs)
        self._coordinator = coordinator

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if (
            isinstance(frame, LLMContextFrame)
            and direction == FrameDirection.DOWNSTREAM
        ):
            try:
                verdict = await self._coordinator.evaluate_input(frame.context)
            except GuardrailEnforcementError as exc:
                await self.push_error(
                    "Guardrail could not safely remove blocked input",
                    exception=exc,
                    fatal=True,
                )
                return
            if verdict.blocked:
                # Do not forward the context, so KB retrieval and the main LLM
                # are both bypassed. The empty response envelope travels
                # through the ordinary pipeline; the downstream response gate
                # inserts the configured redirect and closes the turn.
                await self.push_frame(LLMFullResponseStartFrame(), direction)
                await self.push_frame(LLMFullResponseEndFrame(), direction)
                return
        # Only an allowed context reaches KB retrieval and the main LLM.
        await self.push_frame(frame, direction)


class GuardrailResponseGateProcessor(FrameProcessor):
    """Prevent blocked main-LLM text from reaching TTS or assistant history.

    Function calls execute inside the upstream Pipecat LLM service and remain
    governed by the template's independent tool-approval policy. This processor
    gates spoken response text; it is not a voice function-dispatch gate.
    """

    def __init__(self, coordinator: GuardrailCoordinator, **kwargs):
        super().__init__(**kwargs)
        self._coordinator = coordinator
        self._sentence_aggregator = self._new_sentence_aggregator()
        self._in_response = False
        self._response_blocked = False
        self._response_id = 0
        self._evaluation_tail: asyncio.Task[None] | None = None
        self._delivery_tail: asyncio.Task[None] | None = None

    @staticmethod
    def _new_sentence_aggregator() -> SimpleTextAggregator:
        # Use TTSService's sentence-boundary implementation. Output guarding
        # intentionally imposes sentence-level release even when a provider is
        # otherwise configured for token streaming.
        return SimpleTextAggregator(aggregation_type=AggregationType.SENTENCE)

    def _reset_response(self) -> None:
        self._sentence_aggregator = self._new_sentence_aggregator()
        self._in_response = False
        self._response_blocked = False
        self._evaluation_tail = None
        self._delivery_tail = None

    async def _cancel_output_work(self) -> None:
        """Cancel work from an interrupted/superseded LLM response."""
        self._response_id += 1
        tasks = list(
            {
                task
                for task in (self._evaluation_tail, self._delivery_tail)
                if task is not None
            }
        )
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._evaluation_tail = None
        self._delivery_tail = None

    async def _drain_output_work(self) -> None:
        """Wait for ordered verdicts and downstream sentence delivery."""
        evaluation_tail = self._evaluation_tail
        if evaluation_tail is not None:
            await asyncio.gather(evaluation_tail, return_exceptions=True)
        # Evaluations enqueue deliveries, so capture this tail only after every
        # verdict has completed.
        delivery_tail = self._delivery_tail
        if delivery_tail is not None:
            await asyncio.gather(delivery_tail, return_exceptions=True)

    def _queue_delivery(
        self, frame: Frame, direction: FrameDirection, response_id: int
    ) -> None:
        """Deliver approved sentences in order without blocking evaluation."""
        previous = self._delivery_tail

        async def deliver() -> None:
            if previous is not None:
                await previous
            if response_id != self._response_id or not self._in_response:
                return
            await self.push_frame(frame, direction)

        task = asyncio.create_task(
            deliver(), name=f"guardrail:output-delivery:{response_id}"
        )
        self._delivery_tail = task

    async def _push_redirect(self, message: str, direction: FrameDirection) -> None:
        if message.strip():
            # The redirect is already a complete, trusted utterance. Marking it
            # as sentence-aggregated lets TTS speak immediately instead of
            # waiting for another token or the response-end frame as lookahead.
            await self.push_frame(
                AggregatedTextFrame(
                    text=message.strip(),
                    aggregated_by=AggregationType.SENTENCE,
                ),
                direction,
            )

    async def _block_for_input(
        self,
        direction: FrameDirection,
    ) -> None:
        self._response_blocked = True
        self._sentence_aggregator = self._new_sentence_aggregator()
        if self._coordinator.claim_redirect():
            input_config = self._coordinator.input_config
            if input_config is not None:
                await self._push_redirect(input_config.redirect_message, direction)
        logger.info("Emitted fixed redirect after input guardrail block")

    @staticmethod
    def _redirect_frame(message: str) -> AggregatedTextFrame:
        return AggregatedTextFrame(
            text=message.strip(),
            aggregated_by=AggregationType.SENTENCE,
        )

    async def _evaluate_output_sentence(
        self, sentence: str, direction: FrameDirection, response_id: int
    ) -> None:
        """Evaluate and release one TTS-sized sentence in response order."""
        if self._response_blocked or not sentence.strip():
            return

        output_verdict = await self._coordinator.evaluate_output(sentence)
        if output_verdict.blocked and output_verdict.evaluation_failed:
            # Evaluation was unavailable (timeout/provider error), not a policy
            # decision. Withhold only this unevaluated sentence and keep the
            # rest of the response flowing — a full stop plus redirect here
            # would truncate correct answers whenever the guard model is slow.
            # The verdict is already counted under failed_closed in metrics.
            logger.warning(
                "Withheld one sentence after output guardrail evaluation failure"
            )
            return
        if output_verdict.blocked:
            self._response_blocked = True
            self._sentence_aggregator = self._new_sentence_aggregator()
            output_config = self._coordinator.output_config
            if output_config is not None:
                self._queue_delivery(
                    self._redirect_frame(output_config.redirect_message),
                    direction,
                    response_id,
                )
            logger.info("Discarded sentence after output guardrail block")
            return

        # Bypass TTSService's own text aggregator. The sentence was produced by
        # that exact aggregation implementation above, so it is safe to send
        # straight to synthesis without incurring a second lookahead delay.
        self._queue_delivery(
            AggregatedTextFrame(
                text=sentence,
                aggregated_by=AggregationType.SENTENCE,
            ),
            direction,
            response_id,
        )

    def _queue_output_sentence(self, sentence: str, direction: FrameDirection) -> None:
        """Queue one sentence behind prior verdicts, not behind LLM tokens."""
        if self._response_blocked or not sentence.strip():
            return
        previous = self._evaluation_tail
        response_id = self._response_id

        async def evaluate() -> None:
            if previous is not None:
                await previous
            if response_id != self._response_id or self._response_blocked:
                return
            await self._evaluate_output_sentence(sentence, direction, response_id)

        task = asyncio.create_task(
            evaluate(), name=f"guardrail:output-evaluation:{response_id}"
        )
        self._evaluation_tail = task

    async def _process_output_text(self, text: str, direction: FrameDirection) -> None:
        if self._response_blocked:
            return
        async for aggregation in self._sentence_aggregator.aggregate(text):
            self._queue_output_sentence(aggregation.text, direction)
            if self._response_blocked:
                return

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, (InterruptionFrame, CancelFrame, EndFrame)):
            await self._cancel_output_work()
            self._reset_response()
            await self.push_frame(frame, direction)
            return

        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseStartFrame):
            await self._cancel_output_work()
            self._reset_response()
            self._in_response = True
            # Preserve the normal response lifecycle. No text or audio is
            # released by this boundary frame alone.
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMTextFrame) and self._in_response:
            if (
                self._response_blocked
                or self._coordinator.current_input_verdict.blocked
            ):
                return
            if self._coordinator.output_enabled:
                await self._process_output_text(frame.text, direction)
                return
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseEndFrame) and self._in_response:
            if self._coordinator.output_enabled:
                # Pipecat TTS flushes an unterminated final fragment on this
                # frame. Mirror that behavior so the last fragment is guarded
                # and released at exactly the same boundary.
                if not self._response_blocked:
                    remaining = await self._sentence_aggregator.flush()
                    if remaining is not None:
                        self._queue_output_sentence(remaining.text, direction)
                await self._drain_output_work()

            input_verdict = self._coordinator.current_input_verdict
            if input_verdict.blocked and not self._response_blocked:
                await self._block_for_input(direction)

            await self.push_frame(frame, direction)
            self._reset_response()
            return

        await self.push_frame(frame, direction)
