from datetime import datetime

from google.genai.types import GenerateContentConfig
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.llm_service import FunctionCallParams

from app.core.config.static import GEMINI_API_KEY, GEMINI_SEARCH_RESULT_API_MODEL
from app.core.logger import logger

# ---------- 1. Set up Gemini (Google) LLM for search-only use ----------
search_tool = {"google_search": {}}
tools_list = [search_tool]

gemini_llm: GoogleLLMService | None = None
if GEMINI_API_KEY:
    gemini_llm = GoogleLLMService(
        api_key=GEMINI_API_KEY,
        model=GEMINI_SEARCH_RESULT_API_MODEL,
        tools=tools_list,
    )
else:
    logger.warning("GEMINI_API_KEY not set — web search tool will be unavailable")


# ---------- 2. Define a function that uses Gemini to perform web search ----------
async def gemini_search_fn(params: FunctionCallParams):
    query = params.arguments.get("query")
    if not query or not query.strip():
        logger.warning("Gemini search called with an empty query.")
        await params.result_callback({"error": "Search query cannot be empty."})
        return

    if gemini_llm is None:
        logger.warning("Gemini search unavailable — GEMINI_API_KEY not configured")
        await params.result_callback({"error": "Web search is not configured."})
        return

    logger.info(f"Performing Gemini search for query: {query}")

    try:
        # Pipecat 1.0 removed GoogleLLMContext (universal LLMContext now). We
        # call the underlying Google genai client directly with native content
        # format — this bypass is intentional, the Gemini search path was
        # already side-stepping pipecat's context-aggregation pipeline.
        current_date = datetime.now().strftime("%Y-%m-%d")
        system_instruction = (
            f"You are a helpful assistant that will always search the web for up-to-date information. "
            f"The current date is {current_date}. "
            f"Your responses must be concise—no more than 50 words—while maximizing useful detail and clarity. "
            f"Prioritize relevance, freshness (relative to current date: {current_date}), and specificity. "
            f"Avoid filler or repetition. Get straight to the point."
        )

        config = GenerateContentConfig(
            tools=gemini_llm._tools,
            system_instruction=system_instruction,
        )

        contents = [{"role": "user", "parts": [{"text": query}]}]
        response = await gemini_llm._client.aio.models.generate_content_stream(
            model=gemini_llm._model_name,
            contents=contents,
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
            logger.warning(
                f"Gemini search for query '{query}' returned no text results."
            )
            await params.result_callback(
                {"results": "No information found for your query."}
            )

    except Exception as e:
        logger.error(
            f"Tool Error: [search_web] Error during Gemini search: {e}", exc_info=True
        )
        await params.result_callback({"Tool Error: [search_web] error": str(e)})


# ---------- 3. Define a search tool schema for GPT ----------
search_tool_function = FunctionSchema(
    name="search_web",
    description="Search the web for up-to-date information.",
    properties={"query": {"type": "string", "description": "Search query"}},
    required=["query"],
)

tools = ToolsSchema(
    standard_tools=[
        search_tool_function,
    ]
)

tool_functions = {
    "search_web": gemini_search_fn,
}
