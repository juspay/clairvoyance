"""Provider-specific LLM calls for observers.

Same ``isinstance`` dispatch pattern as ``chat/llm_driver.py``.
Each provider makes a non-streaming API call with tools and returns
(tool_name, tool_args) or (None, None) if no tool was called.
"""

import json
from typing import Any, List

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.openai.base_llm import BaseOpenAILLMService

from app.core.logger import logger

ToolCallResult = tuple[str | None, dict | None]


async def call_llm(
    llm_service: Any,
    transcript_text: str,
    system_prompt: str,
    tools: List[FunctionSchema],
    observer_name: str,
) -> ToolCallResult:
    """Dispatch to provider-specific function calling.

    Returns (tool_name, tool_args) or (None, None) if no tool call.
    """
    if isinstance(llm_service, BaseOpenAILLMService):
        return await _call_openai(
            llm_service, transcript_text, system_prompt, tools, observer_name
        )
    if isinstance(llm_service, AnthropicLLMService):
        return await _call_anthropic(
            llm_service, transcript_text, system_prompt, tools, observer_name
        )
    if isinstance(llm_service, GoogleLLMService):
        return await _call_google(
            llm_service, transcript_text, system_prompt, tools, observer_name
        )
    raise TypeError(f"Unsupported LLM service type: {type(llm_service).__name__}")


async def _call_openai(
    svc: BaseOpenAILLMService,
    transcript_text: str,
    system_prompt: str,
    tools: List[FunctionSchema],
    observer_name: str,
) -> ToolCallResult:
    """Mirror BaseOpenAILLMService._process_context minus the frame layer."""
    context = LLMContext()
    context.add_message({"role": "user", "content": transcript_text})
    context.set_tools(ToolsSchema(standard_tools=tools))
    context.set_tool_choice("auto")

    adapter = svc.get_llm_adapter()
    invocation_params = adapter.get_llm_invocation_params(
        context,
        system_instruction=system_prompt,
        convert_developer_to_user=not svc.supports_developer_role,
    )
    params = svc.build_chat_completion_params(invocation_params)
    params["stream"] = False
    params.pop("stream_options", None)

    response = await svc._client.chat.completions.create(**params)

    message = response.choices[0].message
    if not message.tool_calls:
        logger.debug(
            f"Observer '{observer_name}' no tool call, content='{message.content}'"
        )
        return None, None

    for tool_call in message.tool_calls:
        args = json.loads(tool_call.function.arguments)
        return tool_call.function.name, args
    return None, None


async def _call_anthropic(
    svc: AnthropicLLMService,
    transcript_text: str,
    system_prompt: str,
    tools: List[FunctionSchema],
    observer_name: str,
) -> ToolCallResult:
    """Mirror AnthropicLLMService._process_context minus the frame layer."""
    context = LLMContext()
    context.add_message({"role": "user", "content": transcript_text})
    context.set_tools(ToolsSchema(standard_tools=tools))

    adapter = svc.get_llm_adapter()
    invocation_params = adapter.get_llm_invocation_params(
        context,
        system_instruction=system_prompt,
    )

    params: dict[str, Any] = {
        "model": str(svc._settings.model),
        "messages": invocation_params["messages"],
        "system": invocation_params["system"],
        "tools": invocation_params["tools"] or [],
        "tool_choice": {"type": "auto"},
        "max_tokens": svc._settings.max_tokens or 256,
        "stream": False,
    }
    response = await svc._client.beta.messages.create(**params)

    for block in response.content:
        if block.type == "tool_use":
            return block.name, block.input or {}

    logger.debug(f"Observer '{observer_name}' no tool_use block in response")
    return None, None


async def _call_google(
    svc: GoogleLLMService,
    transcript_text: str,
    system_prompt: str,
    tools: List[FunctionSchema],
    observer_name: str,
) -> ToolCallResult:
    """Mirror GoogleLLMService._process_context minus the frame layer."""
    from google.genai.types import GenerateContentConfig

    context = LLMContext()
    context.add_message({"role": "user", "content": transcript_text})
    context.set_tools(ToolsSchema(standard_tools=tools))

    adapter = svc.get_llm_adapter()
    invocation_params = adapter.get_llm_invocation_params(
        context,
        system_instruction=system_prompt,
    )

    generation_params = svc._build_generation_params(
        system_instruction=invocation_params["system_instruction"],
        tools=invocation_params["tools"] or [],
        tool_config=None,
    )
    generation_params["max_output_tokens"] = svc._settings.max_tokens

    model_name = str(svc._settings.model)
    response = await svc._client.aio.models.generate_content(
        model=model_name,
        contents=invocation_params["messages"],
        config=GenerateContentConfig(**generation_params),
    )

    if response.candidates and response.candidates[0].content:
        for part in response.candidates[0].content.parts or []:
            if part.function_call:
                return part.function_call.name, part.function_call.args or {}

    logger.debug(f"Observer '{observer_name}' no function_call in response")
    return None, None
