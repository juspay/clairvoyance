"""
Knowledge Retrieval Processor (auto_retrieve mode)

Sits between the user context aggregator and the LLM service (agent mode
only). On each genuine user turn it runs hybrid retrieval against the
template's knowledge bases and injects a transient "## Related knowledge"
system message into the context BEFORE this turn's inference.

Pipeline position:
    ... → user_aggregator → KnowledgeRetrievalProcessor → llm → ...

Why this position (verified against pipecat 1.1.0):
- The user aggregator pushes an LLMContextFrame downstream the moment a turn
  ends; the LLM service starts inference on receipt. Intercepting that frame
  is the only synchronous pre-inference hook (aggregator events fire after
  the frame is already en route).
- Interruption safety comes free: InterruptionFrame runs on the high-priority
  system-frame task, which cancels and recreates this processor's process
  task — an in-flight retrieval await is cancelled and the stale frame
  dropped. The user message already lives in the shared LLMContext, so the
  next turn-stop pushes a fresh frame with full history. Context mutation +
  push below are synchronous back-to-back, so cancellation can never leave a
  half-updated context.
- Post-tool re-inference travels UPSTREAM from the assistant aggregator and
  never passes this position, so tool cycles don't re-trigger retrieval.

Retrieval is fail-open (see services/knowledge_base.py): timeout/error means
the frame is pushed through unmodified.
"""

from typing import Any, Dict, List, Optional, Tuple

from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.ai.voice.agents.breeze_buddy.services.knowledge_base import (
    KnowledgeBaseConfig,
    build_retrieval_query,
    fetch_kb_context_message,
)
from app.core.logger import logger


class KnowledgeRetrievalProcessor(FrameProcessor):
    """Injects per-turn KB context into LLMContextFrames (transiently)."""

    def __init__(self, config: KnowledgeBaseConfig, **kwargs):
        """One instance per call, constructed in ``Agent.run`` when the
        resolved runtime mode is auto_retrieve, and handed to
        ``build_pipeline`` for insertion before the LLM service."""
        super().__init__(**kwargs)
        self._config = config
        # Identity handle of the message injected last turn — replaced (not
        # accumulated) each turn, so at most one KB message exists in history.
        self._last_injection: Optional[Dict[str, Any]] = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Pipecat entry point for every frame passing this position.

        Everything except LLMContextFrame passes straight through. For a
        genuine user turn: retrieve (cancellable await, fail-open) ->
        replace last turn's injection -> push. See the module docstring for
        why the mutation+push tail must stay await-free.
        """
        await super().process_frame(frame, direction)

        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return

        query, recent_turns = self._extract_user_query(frame.context)
        if query is None:
            # Node-transition / greeting / idle-nudge runs — no retrieval.
            await self.push_frame(frame, direction)
            return

        retrieval_query = build_retrieval_query(query, recent_turns, self._config)
        # Cancellable await; the context has NOT been touched yet.
        kb_message = await fetch_kb_context_message(self._config, retrieval_query)

        # --- No awaits between here and push_frame (atomic wrt cancellation) ---
        self._replace_injection(frame.context, kb_message)
        await self.push_frame(frame, direction)

    def _extract_user_query(
        self, context: LLMContext
    ) -> Tuple[Optional[str], Optional[List[str]]]:
        """Return (current_user_utterance, recent_prior_user_turns).

        Retrieval fires only when the last context message is a plain-string
        user message — which is exactly the shape of a finished user turn.
        Non-dict entries (LLMSpecificMessage wrappers) are tolerated.
        """
        try:
            messages = context.get_messages()
        except Exception as e:
            logger.warning(f"KB processor could not read context messages: {e}")
            return None, None
        if not messages:
            return None, None

        last = messages[-1]
        if not isinstance(last, dict) or last.get("role") != "user":
            return None, None
        content = last.get("content")
        if not isinstance(content, str) or not content.strip():
            return None, None

        recent: List[str] = []
        if self._config.include_recent_turns > 0:
            for message in messages[:-1]:
                if (
                    isinstance(message, dict)
                    and message.get("role") == "user"
                    and isinstance(message.get("content"), str)
                    and message is not self._last_injection
                ):
                    recent.append(message["content"])
        return content, recent

    def _replace_injection(
        self, context: LLMContext, kb_message: Optional[Dict[str, Any]]
    ) -> None:
        """Drop last turn's injected message; insert this turn's (if any)
        immediately before the final user message."""
        previous = self._last_injection

        def _transform(messages):
            # Runs inside context.transform_messages: filter by object
            # identity (never equality — role/content dicts can repeat),
            # then insert the new KB message just before the trailing user
            # message so it reads as fresh reference material.
            filtered = [m for m in messages if m is not previous]
            if kb_message is not None:
                insert_at = len(filtered)
                if filtered and isinstance(filtered[-1], dict):
                    if filtered[-1].get("role") == "user":
                        insert_at -= 1
                filtered.insert(insert_at, kb_message)
            return filtered

        try:
            context.transform_messages(_transform)
            self._last_injection = kb_message
        except Exception as e:
            logger.warning(f"KB processor failed to inject context: {e}")
