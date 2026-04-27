"""
RAG Context Processor for Breeze Buddy.

Sits **after** the user_aggregator in the pipeline.  When the user_aggregator
pushes a ``LLMContextFrame`` downstream (i.e. the user's turn has been
aggregated and the context is ready to be sent to the LLM), this processor:

1. Extracts the latest user utterance directly from the ``LLMContextFrame``
   (the last ``user`` role message already assembled by ``user_aggregator``).
2. Embeds it and fetches relevant knowledge from the ``RagMemoryRouter``
2. If knowledge is found it is injected **directly into the LLMContext object**
   as a temporary ``system`` message — before the context reaches the LLM.
3. After the LLM finishes responding (``LLMFullResponseEndFrame``), the
   injected message is **removed from the context** so it does not pollute the
   persistent conversation history and does not confuse subsequent turns.

Why this approach:
- The knowledge never touches the user's ``TranscriptionFrame`` text.
- The knowledge is not a persistent instruction — it disappears after one turn.
- Because the LLM sees a plain ``system`` message with *only* factual content
  (no "do this / don't do that" meta-instructions), the node's own task/role
  messages remain the sole source of behavioural guidance.  The LLM answers the
  side question from the facts and then continues its current task.

Pipeline position::

    transport.input()
    → stt
    → TranscriptionGateProcessor
    → user_aggregator              ← produces LLMContextFrame
    → RagContextProcessor          ← here: enriches context ephemerally
    → llm
    → tts
    → transport.output()
    → context_aggregator.assistant()
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
)
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.ai.voice.agents.breeze_buddy.services.rag.memory_router import RagMemoryRouter
from app.core.config.static import RAG_MAX_CONTEXT_CHARS
from app.core.logger import logger

# Role used for the ephemeral RAG message — must match what we look for on cleanup
_RAG_ROLE = "system"
_RAG_MARKER = "[RAG]"


class RagContextProcessor(FrameProcessor):
    """Ephemerally enriches the LLM context with RAG knowledge on each user turn.

    The retrieved knowledge is injected into the ``LLMContext`` object directly
    before it reaches the LLM and removed immediately after the LLM responds.
    This keeps the persistent conversation history clean and avoids competing
    with the node's own task/role messages.

    Args:
        rag_router: Fully initialised ``RagMemoryRouter`` for this call.
    """

    def __init__(self, rag_router: RagMemoryRouter) -> None:
        super().__init__()
        self._router = rag_router
        # The LLMContext we injected into (kept for cleanup after LLM responds)
        self._injected_context: Optional[OpenAILLMContext] = None

    # ------------------------------------------------------------------
    # Frame processing
    # ------------------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            await self._enrich_and_forward(frame, direction)
            return

        if isinstance(frame, LLMFullResponseEndFrame):
            # LLM finished — remove the ephemeral RAG message from context
            self._cleanup_rag_message()
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _enrich_and_forward(
        self, frame: LLMContextFrame, direction: FrameDirection
    ) -> None:
        """Fetch RAG context and inject it into the LLMContext before forwarding."""
        llm_context: OpenAILLMContext = frame.context  # type: ignore[assignment]

        # Extract the latest user utterance from the context messages
        utterance = ""
        messages = llm_context.get_messages() or []
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    utterance = content.strip()
                elif isinstance(content, list):
                    # OpenAI multi-part content: extract text parts
                    parts = [
                        p.get("text", "")
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    utterance = " ".join(parts).strip()
                break

        if not utterance:
            await self.push_frame(frame, direction)
            return

        # Retrieve knowledge (cache hit ≈ 0.1 ms, miss ≈ 50–150 ms)
        try:
            context = await self._router.get_context(utterance)
        except Exception as exc:
            logger.warning("RagContextProcessor: context retrieval failed: %s", exc)
            await self.push_frame(frame, direction)
            return

        if not context:
            await self.push_frame(frame, direction)
            return

        # Trim to prevent prompt bloat
        if len(context) > RAG_MAX_CONTEXT_CHARS:
            context = context[:RAG_MAX_CONTEXT_CHARS].rsplit("\n", 1)[0]

        # Inject directly into the LLMContext object as an ephemeral system msg.
        # The framing instruction tells the LLM to use the retrieved facts to
        # answer the user's question.  The marker prefix lets us find and remove
        # it on cleanup.
        rag_message: Dict[str, Any] = {
            "role": _RAG_ROLE,
            "content": (
                f"{_RAG_MARKER} The following are relevant facts from the knowledge base. "
                "Use them to answer the user's question.\n\n"
                f"{context}"
            ),
        }

        llm_context.add_message(rag_message)  # type: ignore[arg-type]
        self._injected_context = llm_context

        logger.debug(
            "RagContextProcessor: injected %d chars of RAG context for '%s…'",
            len(context),
            utterance[:40],
        )

        await self.push_frame(frame, direction)

    def _cleanup_rag_message(self) -> None:
        """Remove the ephemeral RAG system message from the LLMContext."""
        if self._injected_context is None:
            return

        messages = self._injected_context.get_messages()
        if not messages:
            self._injected_context = None
            return

        original_len = len(messages)
        filtered = []
        for m in messages:
            content = m.get("content")
            is_rag_msg = (
                m.get("role") == _RAG_ROLE
                and isinstance(content, str)
                and content.startswith(_RAG_MARKER)
            )
            if not is_rag_msg:
                filtered.append(m)

        if len(filtered) < original_len:
            self._injected_context.set_messages(filtered)
            logger.debug(
                "RagContextProcessor: cleaned up %d ephemeral RAG message(s)",
                original_len - len(filtered),
            )

        self._injected_context = None
