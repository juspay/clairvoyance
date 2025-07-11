import os

from app.core.logger import logger
from app.core.config import GEMINI_SEARCH_RESULT_API_MODEL, GEMINI_API_KEY
from pipecat.frames.frames import LLMMessagesFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.services.google.llm import GoogleLLMService, GoogleLLMContext
from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.frame_processor import FrameDirection
from google.genai.types import GenerateContentConfig



# ---------- 1. Set up Gemini (Google) LLM for search-only use ----------
search_tool = {"google_search": {}}
tools_list = [search_tool]

gemini_llm = GoogleLLMService(
    api_key=GEMINI_API_KEY,
    model=GEMINI_SEARCH_RESULT_API_MODEL,
    tools=tools_list,
)

# ---------- 2. Define a function that uses Gemini to perform web search ----------
async def gemini_search_fn(params: FunctionCallParams):
    query = params.arguments.get("query")
    if not query or not query.strip():
        logger.warning("Gemini search called with an empty query.")
        await params.result_callback({"error": "Search query cannot be empty."})
        return

    logger.info(f"Performing Gemini search for query: {query}")

    try:
        ctx = GoogleLLMContext(messages=[
            {"role": "user", "content": query}
        ])

        ctx.system_message = (
            "You are a helpful assistant that will always search the web for up-to-date information. "
            "Your responses must be concise—no more than 50 words—while maximizing useful detail and clarity. "
            "Prioritize relevance, freshness, and specificity. Avoid filler or repetition. Get straight to the point."
        )

        ctx._restructure_from_openai_messages()

        config = GenerateContentConfig(
            tools=gemini_llm._tools,
            system_instruction=ctx.system_message,
        )

        response = await gemini_llm._client.aio.models.generate_content_stream(
            model=gemini_llm._model_name,
            contents=ctx.messages,
            config=config,
        )

        # Collect the generated text
        result_parts = []

        async for chunk in response:
            for candidate in chunk.candidates:
                for part in candidate.content.parts:
                    if part.text:
                        result_parts.append(part.text)

        full_result = "".join(result_parts).strip()
        
        if full_result:
            logger.info(f"Gemini search for '{query}' returned: {full_result[:100]}...")
            await params.result_callback({"results": full_result})
        else:
            logger.warning(f"Gemini search for query '{query}' returned no text results.")
            await params.result_callback({"results": "No information found for your query."})

    except Exception as e:
        logger.error(f"Error during Gemini search: {e}", exc_info=True)
        await params.result_callback({"error": str(e)})


# ---------- 3. Define a search tool schema for GPT ----------
search_tool_function = FunctionSchema(
    name="search_web",
    description="Search the web for up-to-date information.",
    properties={
        "query": {"type": "string", "description": "Search query"}
    },
    required=["query"]
)

tools = ToolsSchema(
    standard_tools=[
        search_tool_function,
    ]
)

tool_functions = {
    "search_web": gemini_search_fn,
}