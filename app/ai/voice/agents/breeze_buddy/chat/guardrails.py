"""Chat-specific adapters over the shared Guardrail coordinator."""

from dataclasses import dataclass, field
from typing import Optional

from pipecat.utils.text.base_text_aggregator import AggregationType
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator

from app.ai.voice.agents.breeze_buddy.guardrails.evaluator import (
    GuardrailCoordinator,
    GuardrailVerdict,
)
from app.core.logger import logger


@dataclass
class GuardedTextResult:
    """Approved text released by one feed/flush operation."""

    chunks: list[str] = field(default_factory=list)
    blocked: bool = False
    verdict: Optional[GuardrailVerdict] = None


class ChatOutputGuard:
    """Release chat prose only after sentence-level output approval."""

    def __init__(
        self,
        coordinator: Optional[GuardrailCoordinator],
        *,
        released_any: bool = False,
    ) -> None:
        self._coordinator = coordinator
        self._aggregator = SimpleTextAggregator(
            aggregation_type=AggregationType.SENTENCE
        )
        self._blocked = False
        self._released_any = released_any

    @property
    def enabled(self) -> bool:
        return bool(self._coordinator and self._coordinator.output_enabled)

    @property
    def blocked(self) -> bool:
        return self._blocked

    @property
    def redirect_message(self) -> str:
        config = self._coordinator.output_config if self._coordinator else None
        return config.redirect_message.strip() if config is not None else ""

    async def feed(self, text: str) -> GuardedTextResult:
        if self._blocked or not text:
            return GuardedTextResult(blocked=self._blocked)
        if not self.enabled:
            return GuardedTextResult(chunks=[text])

        result = GuardedTextResult()
        async for aggregation in self._aggregator.aggregate(text):
            verdict = await self._evaluate(aggregation.text)
            if verdict.blocked and verdict.evaluation_failed:
                # Evaluation unavailable (timeout/provider error), not a policy
                # decision: withhold only this unevaluated sentence and keep
                # streaming the rest instead of ending the whole turn.
                logger.warning(
                    "Withheld one chat sentence after output guardrail "
                    "evaluation failure"
                )
                continue
            if verdict.blocked:
                self._blocked = True
                result.blocked = True
                result.verdict = verdict
                await self._aggregator.reset()
                break
            result.chunks.append(self._release(aggregation.text))
        return result

    async def flush(self) -> GuardedTextResult:
        if self._blocked or not self.enabled:
            return GuardedTextResult(blocked=self._blocked)
        remaining = await self._aggregator.flush()
        if remaining is None or not remaining.text.strip():
            return GuardedTextResult()
        verdict = await self._evaluate(remaining.text)
        if verdict.blocked and verdict.evaluation_failed:
            # Same policy as feed(): an unavailable evaluation withholds only
            # the unevaluated fragment; it does not end the turn.
            logger.warning(
                "Withheld final chat fragment after output guardrail "
                "evaluation failure"
            )
            return GuardedTextResult()
        if verdict.blocked:
            self._blocked = True
            return GuardedTextResult(blocked=True, verdict=verdict)
        return GuardedTextResult(chunks=[self._release(remaining.text)])

    async def _evaluate(self, sentence: str) -> GuardrailVerdict:
        assert self._coordinator is not None
        return await self._coordinator.evaluate_output(sentence)

    def _release(self, sentence: str) -> str:
        # SimpleTextAggregator trims boundary spaces. Restore one separator
        # between approved sentences so SSE concatenation remains readable.
        released = sentence
        if self._released_any and sentence and not sentence[0].isspace():
            released = f" {sentence}"
        self._released_any = True
        return released


__all__ = ["ChatOutputGuard", "GuardedTextResult"]
