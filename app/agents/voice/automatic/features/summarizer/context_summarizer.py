# app/agents/voice/automatic/context_summarizer.py
import asyncio
from typing import List, Dict, Any, Optional
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.frames.frames import Frame, LLMMessagesFrame
from app.core.config import KEEP_RECENT_TURNS, MAX_TURNS_BEFORE_SUMMARY
from app.core.logger import logger

class ContextSummarizer(OpenAILLMContext):
    """
    Extended OpenAI LLM Context that automatically summarizes conversation
    after a specified number of turns to maintain context window efficiency.
    """

    def __init__(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        max_turns_before_summary: int = MAX_TURNS_BEFORE_SUMMARY,
        keep_recent_turns: int = KEEP_RECENT_TURNS,
        enable_summarization: bool = True,
        llm_service=None
    ):
        super().__init__(messages, tools)
        self._max_turns_before_summary = max_turns_before_summary
        self._keep_recent_turns = keep_recent_turns
        self._enable_summarization = enable_summarization
        self._turn_count = 0
        self._llm_service = llm_service
        self._is_summarizing = False
        self._original_system_message = messages[0] if messages and messages[0]["role"] == "system" else None

    def add_message(self, message: Dict[str, Any]):
        """Adds a message to the context and increments the turn count if it's a user message."""
        super().add_message(message)
        if message["role"] == "user":
            self._turn_count += 1
            logger.debug(f"--- Summarizer: Turn count incremented to: {self._turn_count} ---")
            asyncio.create_task(self._check_if_summary_needed())

    async def _check_if_summary_needed(self):
        """Checks if the turn count has reached the threshold to trigger summarization."""
        if self._enable_summarization and not self._is_summarizing and self._turn_count >= self._max_turns_before_summary:
            await self._summarize_context()

    async def _summarize_context(self):
        """Performs the summarization of the conversation history."""
        self._is_summarizing = True
        try:
            conversation_messages = [msg for msg in self._messages if msg["role"] in ["user", "assistant", "tool"]]
            if len(conversation_messages) < self._keep_recent_turns * 2:
                return

            # Determine which messages to keep and which to summarize
            messages_to_keep = []
            user_turns_to_keep = 0
            for msg in reversed(conversation_messages):
                messages_to_keep.insert(0, msg)
                if msg["role"] == "user":
                    user_turns_to_keep += 1
                    if user_turns_to_keep >= self._keep_recent_turns:
                        break
            
            messages_to_summarize = [msg for msg in conversation_messages if msg not in messages_to_keep]
            if not messages_to_summarize:
                return

            # Find previous summary
            previous_summary = ""
            for msg in self._messages:
                if msg["role"] == "system" and "Previous conversation summary:" in msg["content"]:
                    previous_summary = msg["content"].replace("Previous conversation summary:", "").strip()
                    break

            # Create summarization prompt
            prompt_parts = []
            if previous_summary:
                prompt_parts.append(f"Current summary of the conversation so far:\n{previous_summary}\n\nPlease create a new, updated summary that incorporates the following new messages:\n")
            else:
                prompt_parts.append("Summarize the key points of this conversation, focusing on decisions, user preferences, and important outcomes. Conversation:\n")

            for msg in messages_to_summarize:
                role = "User" if msg["role"] == "user" else "Assistant"
                content = msg.get("content", f"[{msg.get('tool_calls', [{}])[0].get('function', {}).get('name', 'tool call')}]")
                if content:
                    prompt_parts.append(f"\n{role}: {content}")

            summary_messages = [
                {"role": "system", "content": "You are a helpful assistant that creates concise conversation summaries. Your primary goal is to maintain a perfect, long-term memory of the conversation. It is absolutely crucial that you preserve all specific details provided by the user. Also, preserve any mentioned dates or time ranges accurately."},
                {"role": "user", "content": "".join(prompt_parts)}
            ]

            # Get summary from LLM
            summary_context = OpenAILLMContext(messages=summary_messages)
            chunks = await self._llm_service.get_chat_completions(summary_context, summary_messages)
            summary_parts = [chunk.choices[0].delta.content async for chunk in chunks if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content]
            summary = "".join(summary_parts)

            if not summary:
                logger.warning("Summary generation resulted in empty content.")
                return

            logger.debug(f"--- Summarizer: Generated summary: {summary} ---")

            # Reconstruct messages
            new_messages = []
            if self._original_system_message:
                new_messages.append(self._original_system_message)
            
            new_messages.append({"role": "system", "content": f"Previous conversation summary: {summary}"})
            new_messages.extend(messages_to_keep)
            logger.debug(f"New Context to LLm is: {new_messages}")
            self._messages = new_messages
            self._turn_count = 0
        except Exception as e:
            logger.error(f"Error during summarization: {e}")
        finally:
            self._is_summarizing = False
