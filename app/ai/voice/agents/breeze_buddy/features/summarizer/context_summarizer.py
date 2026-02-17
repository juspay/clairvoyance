import asyncio
from typing import Any, Dict, List, Optional, cast

from openai.types.chat import ChatCompletionMessageParam
from pipecat.adapters.services.open_ai_adapter import OpenAILLMInvocationParams
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext

from app.core.config.static import KEEP_RECENT_TURNS, MAX_TURNS_BEFORE_SUMMARY
from app.core.logger import logger


class ContextSummarizer(OpenAILLMContext):
    """
    Extended OpenAI LLM Context that automatically summarizes conversation
    after a specified number of turns to maintain context window efficiency.

    This implementation is specifically tailored for Breeze Buddy voice agents
    to handle longer conversations efficiently by summarizing older messages
    while preserving recent context.
    """

    def __init__(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        max_turns_before_summary: int = MAX_TURNS_BEFORE_SUMMARY,
        keep_recent_turns: int = KEEP_RECENT_TURNS,
        enable_summarization: bool = True,
        llm_service=None,
    ):
        # Cast messages to the expected type for the parent class
        super().__init__(cast(List[ChatCompletionMessageParam], messages), tools)  # type: ignore
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
        message_dict = cast(Dict[str, Any], message)
        if message_dict.get("role") == "user":
            self._turn_count += 1
            logger.debug(
                f"--- Breeze Buddy Summarizer: Turn count incremented to: {self._turn_count} ---"
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
            # Separate system messages from conversation messages
            system_messages = [msg for msg in self._messages if msg["role"] == "system"]
            conversation_messages = [
                msg
                for msg in self._messages
                if msg["role"] in ["user", "assistant", "tool"]
            ]
            if len(conversation_messages) < self._keep_recent_turns * 2:
                return

            # Log context BEFORE summarization
            logger.info(f"=== BREEZE BUDDY SUMMARIZATION START ===")
            logger.info(
                f"Total messages in context BEFORE summarization: {len(self._messages)}"
            )
            logger.info(
                f"System messages: {len(system_messages)}, Conversation messages: {len(conversation_messages)}"
            )
            logger.debug(f"Context BEFORE summarization:\n{self._messages}")

            # Determine which messages to keep and which to summarize
            messages_to_keep: List[ChatCompletionMessageParam] = []
            user_turns_to_keep = 0
            for msg in reversed(conversation_messages):
                messages_to_keep.insert(0, cast(ChatCompletionMessageParam, msg))
                if msg["role"] == "user":
                    user_turns_to_keep += 1
                    if user_turns_to_keep >= self._keep_recent_turns:
                        break

            messages_to_summarize = [
                msg for msg in conversation_messages if msg not in messages_to_keep
            ]
            if not messages_to_summarize:
                logger.info(
                    f"No messages to summarize (need at least {self._keep_recent_turns * 2} conversation messages)"
                )
                return

            logger.info(
                f"Messages to summarize: {len(messages_to_summarize)}, Messages to keep: {len(messages_to_keep)}"
            )

            # Find previous summary
            previous_summary = ""
            for msg in self._messages:
                if msg["role"] == "system":
                    content = msg.get("content", "")
                    # Handle both string and iterable content types
                    if (
                        isinstance(content, str)
                        and "Previous conversation summary:" in content
                    ):
                        previous_summary = content.replace(
                            "Previous conversation summary:", ""
                        ).strip()
                        break

            # Create summarization prompt
            prompt_parts: List[str] = []
            if previous_summary:
                prompt_parts.append(
                    f"Current summary of the conversation so far:\n{previous_summary}\n\nPlease create a new, updated summary that incorporates the following new messages:\n"
                )
            else:
                prompt_parts.append(
                    "Summarize the key points of this conversation, focusing on customer responses, order details, address confirmations, and important outcomes. Conversation:\n"
                )

            for msg in messages_to_summarize:
                role = "User" if msg["role"] == "user" else "Assistant"
                content = msg.get("content", "")
                if not isinstance(content, str):
                    # Handle tool calls - safely extract tool name
                    tool_calls = msg.get("tool_calls", [])
                    if (
                        tool_calls
                        and isinstance(tool_calls, list)
                        and len(tool_calls) > 0
                    ):
                        first_tool = tool_calls[0]
                        if isinstance(first_tool, dict):
                            function_name = first_tool.get("function", {}).get(
                                "name", "tool call"
                            )
                            content = f"[{function_name}]"
                        else:
                            content = "[tool call]"
                    else:
                        content = "[tool call]"
                if content:
                    prompt_parts.append(f"\n{role}: {content}")

            summary_messages: List[Dict[str, str]] = [
                {
                    "role": "system",
                    "content": """You are a call summarization assistant for an automated calling system.

Summarize the conversation while preserving ALL critical details EXACTLY (names, phone numbers, emails, order IDs, booking IDs, addresses, dates, prices). Do NOT paraphrase these.

Rules:
- Summarize only non-essential dialogue.
- If information is not mentioned, do not invent it.
- If something is unclear, mark it as "Not provided".
- Do NOT assume or hallucinate.
- Clearly capture the customer’s final intent or disposition.
- Keep the summary concise but complete enough for another agent to continue the conversation without asking the customer to repeat information.
- Maintain a professional call-center tone and structured clarity.

IMPORTANT:
- Be EXACT with names, numbers, and addresses - do not paraphrase critical details
- Always address the person by their name respectfully when continuing the conversation
- Preserve ALL important details so the conversation can continue seamlessly without asking for repeated information
- The summary should enable the AI to remember everything important about this person and their requests""",
                },
                {"role": "user", "content": "".join(prompt_parts)},
            ]

            # Get summary from LLM
            if self._llm_service is None:
                logger.warning(
                    "Breeze Buddy Summarizer: LLM service not available for summarization."
                )
                return

            from openai import NOT_GIVEN

            params_from_context = OpenAILLMInvocationParams(
                messages=cast(List[ChatCompletionMessageParam], summary_messages),
                tools=NOT_GIVEN,
                tool_choice=NOT_GIVEN,
            )
            chunks = await self._llm_service.get_chat_completions(params_from_context)
            summary_parts: List[str] = [
                chunk.choices[0].delta.content or ""
                async for chunk in chunks
                if chunk.choices
                and chunk.choices[0].delta
                and chunk.choices[0].delta.content
            ]
            summary = "".join(summary_parts)

            if not summary:
                logger.warning(
                    "Breeze Buddy Summarizer: Summary generation resulted in empty content."
                )
                return

            logger.info(f"✅ Generated summary ({len(summary)} characters):\n{summary}")

            # Reconstruct messages - preserve ALL system messages and add summary
            new_messages: List[ChatCompletionMessageParam] = []

            # Keep all system messages (they contain template instructions and flow guidance)
            for system_msg in system_messages:
                new_messages.append(cast(ChatCompletionMessageParam, system_msg))

            # Add the conversation summary as a new system message
            new_messages.append(
                {
                    "role": "system",
                    "content": f"Previous conversation summary: {summary}",
                }
            )

            # Add recent conversation messages
            new_messages.extend(messages_to_keep)

            # Log context AFTER summarization
            logger.info(f"=== BREEZE BUDDY SUMMARIZATION COMPLETE ===")
            logger.info(
                f"Total messages in context AFTER summarization: {len(new_messages)}"
            )
            logger.info(
                f"Messages reduced from {len(self._messages)} to {len(new_messages)}"
            )
            logger.info(
                f"Space saved: {len(self._messages) - len(new_messages)} messages"
            )
            logger.debug(f"Context AFTER summarization:\n{new_messages}")
            logger.info(f"Turn counter reset from {self._turn_count} to 0")

            self._messages = cast(List[ChatCompletionMessageParam], new_messages)  # type: ignore
            self._turn_count = 0
        except Exception as e:
            logger.error(f"Breeze Buddy Summarizer: Error during summarization: {e}")
        finally:
            self._is_summarizing = False
