# app/agents/voice/automatic/context_summarizer.py
import asyncio
from typing import Any, Dict, List, Optional, Union, cast

from openai._types import NotGiven
from openai.types.chat import ChatCompletionMessageParam
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.adapters.services.open_ai_adapter import OpenAILLMInvocationParams
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext

from app.core.config.static import KEEP_RECENT_TURNS, MAX_TURNS_BEFORE_SUMMARY
from app.core.logger import logger


class ContextSummarizer(OpenAILLMContext):
    """
    Extended OpenAI LLM Context that automatically summarizes conversation
    after a specified number of turns to maintain context window efficiency.
    """

    def __init__(
        self,
        messages: Optional[List[ChatCompletionMessageParam]] = None,
        tools: Optional[Union[List[Dict[str, Any]], ToolsSchema]] = None,
        max_turns_before_summary: int = MAX_TURNS_BEFORE_SUMMARY,
        keep_recent_turns: int = KEEP_RECENT_TURNS,
        enable_summarization: bool = True,
        llm_service=None,
    ):
        # Convert Dict messages to proper ChatCompletionMessageParam if needed
        converted_messages: List[ChatCompletionMessageParam] = []
        if messages:
            for msg in messages:
                converted_messages.append(msg)

        # Handle tools parameter properly
        tools_param = NotGiven()
        if tools is not None:
            if isinstance(tools, list):
                tools_param = (
                    ToolsSchema(standard_tools=[]) if not tools else NotGiven()
                )
            else:
                tools_param = tools

        super().__init__(converted_messages, tools_param)
        self._max_turns_before_summary = max_turns_before_summary
        self._keep_recent_turns = keep_recent_turns
        self._enable_summarization = enable_summarization
        self._turn_count = 0
        self._llm_service = llm_service
        self._is_summarizing = False
        self._original_system_message = (
            messages[0] if messages and messages[0]["role"] == "system" else None
        )

    def add_message(self, message: ChatCompletionMessageParam):
        """Adds a message to the context and increments the turn count if it's a user message."""
        super().add_message(message)
        if isinstance(message, dict) and message.get("role") == "user":
            self._turn_count += 1
            logger.debug(
                f"--- Summarizer: Turn count incremented to: {self._turn_count} ---"
            )
            asyncio.create_task(self._check_if_summary_needed())

    async def _check_if_summary_needed(self):
        """Checks if the turn count has reached the threshold to trigger summarization."""
        if (
            self._enable_summarization
            and not self._is_summarizing
            and self._turn_count >= self._max_turns_before_summary
        ):
            await self._summarize_context()

    async def _summarize_context(self):
        """Performs the summarization of the conversation history."""
        self._is_summarizing = True
        try:
            conversation_messages = [
                msg
                for msg in self._messages
                if msg["role"] in ["user", "assistant", "tool"]
            ]
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

            messages_to_summarize = [
                msg for msg in conversation_messages if msg not in messages_to_keep
            ]
            if not messages_to_summarize:
                return

            # Find previous summary
            previous_summary = ""
            for msg in self._messages:
                if (
                    isinstance(msg, dict)
                    and msg.get("role") == "system"
                    and isinstance(msg.get("content"), str)
                    and "Previous conversation summary:" in msg.get("content", "")
                ):
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        previous_summary = content.replace(
                            "Previous conversation summary:", ""
                        ).strip()
                    break

            # Create summarization prompt
            prompt_parts = []
            if previous_summary:
                prompt_parts.append(
                    f"Current summary of the conversation so far:\n{previous_summary}\n\nPlease create a new, updated summary that incorporates the following new messages:\n"
                )
            else:
                prompt_parts.append(
                    "Summarize the key points of this conversation, focusing on decisions, user preferences, and important outcomes. Conversation:\n"
                )

            for msg in messages_to_summarize:
                role = "User" if msg.get("role") == "user" else "Assistant"
                content = msg.get("content")
                if not content:
                    # Handle tool calls safely
                    tool_calls = msg.get("tool_calls", [])
                    if (
                        tool_calls
                        and isinstance(tool_calls, list)
                        and len(tool_calls) > 0
                    ):
                        tool_call = tool_calls[0]
                        if isinstance(tool_call, dict):
                            function_info = tool_call.get("function", {})
                            if isinstance(function_info, dict):
                                tool_name = function_info.get("name", "tool call")
                                content = f"[{tool_name}]"
                            else:
                                content = "[tool call]"
                        else:
                            content = "[tool call]"
                    else:
                        content = "[unknown content]"
                if content:
                    prompt_parts.append(f"\n{role}: {content}")

            summary_messages = [
                {
                    "role": "system",
                    "content": "You are a helpful assistant that creates concise conversation summaries. Your primary goal is to maintain a perfect, long-term memory of the conversation. It is absolutely crucial that you preserve all specific details provided by the user. Also, preserve any mentioned dates or time ranges accurately.",
                },
                {"role": "user", "content": "".join(prompt_parts)},
            ]

            # Get summary from LLM
            if self._llm_service is None:
                logger.warning("LLM service not available for summarization")
                return

            # Convert to proper ChatCompletionMessageParam format
            formatted_summary_messages: List[ChatCompletionMessageParam] = []
            for msg in summary_messages:
                formatted_summary_messages.append(cast(ChatCompletionMessageParam, msg))

            params_from_context = OpenAILLMInvocationParams(
                messages=formatted_summary_messages, tools=[], tool_choice="none"
            )
            chunks = await self._llm_service.get_chat_completions(params_from_context)
            summary_parts = [
                chunk.choices[0].delta.content
                async for chunk in chunks
                if chunk.choices
                and chunk.choices[0].delta
                and chunk.choices[0].delta.content
            ]
            summary = "".join(summary_parts)

            if not summary:
                logger.warning("Summary generation resulted in empty content.")
                return

            logger.debug(f"--- Summarizer: Generated summary: {summary} ---")

            # Reconstruct messages
            new_messages: List[ChatCompletionMessageParam] = []
            if self._original_system_message:
                new_messages.append(self._original_system_message)

            new_messages.append(
                cast(
                    ChatCompletionMessageParam,
                    {
                        "role": "system",
                        "content": f"Previous conversation summary: {summary}",
                    },
                )
            )
            new_messages.extend(
                cast(List[ChatCompletionMessageParam], messages_to_keep)
            )
            logger.debug(f"New Context to LLm is: {new_messages}")
            self._messages = new_messages
            self._turn_count = 0
        except Exception as e:
            logger.error(f"Error during summarization: {e}")
        finally:
            self._is_summarizing = False
