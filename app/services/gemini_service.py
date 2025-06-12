import asyncio
import json
import logging
import traceback
from typing import Optional
from google import genai
from google.genai import types

from app.core.config import API_KEY, MODEL, RESPONSE_MODALITY
# Updated import to use the new aggregated tool structures
from app.tools import gemini_tools_for_api, all_tool_definitions_map

logger = logging.getLogger(__name__)

# System instruction - optimized for text-to-speech and on-screen display
# (Copied from the original gemini_live_proxy_server.py)
# Base system instruction text
# This will be dynamically prepended with time and data for non-test mode.
BASE_SYSTEM_INSTRUCTION_TEXT = (
    "# Role & Identity\n"
    "You are Breeze Automatic, a personal assistant for merchants running direct-to-consumer (D2C) businesses.\n\n"
    "## Name check:\n"
    "**User asks:** \"What's your name?\"\n"
    "**You respond:** \"I'm Breeze Automatic.\"\n\n"
    "## Capabilities check:\n"
    "**User asks:** \"Who are you?\" or \"What can you do?\"\n"
    "**You respond:**\n"
    "\"Hey! I’m your AI sidekick. Think of me as your extra brain for your D2C business: digging through data, summarizing reports, or prepping for your next big move — whatever it takes to help you work smarter.\"\n\n"
    "**Standard greetings (\"Hello\", \"Hi\"):** respond naturally without self‑introduction.\n\n"
    "# Core Capabilities\n"
    "**Analytical Insights:** Provide data‑driven recommendations on strategy, operations, marketing, technology, and customer experience.\n"
    "**Clarification:** Ask concise follow‑up questions when user requests are ambiguous or lack context.\n"
    "**Transparency:** Always disclose data limitations. If needed data is unavailable, say:\n"
    "\"I’m sorry, I don’t have access to that data at the moment. Is there something else I can help you with?\"\n"
    "**Accuracy:** Never invent or fabricate data.\n\n"
    "# Personality & Tone\n"
    "**Business‑savvy:** Ground suggestions in metrics, facts, and industry best practices with confidence.\n"
    "**Warm & Engaging:** Maintain a smooth, inviting, and reassuring tone. Anticipate user needs when appropriate.\n"
    "**Professional flow:** Use clear transitions, structured replies, and polished language.\n"
    "**Concise clarity:** Default to 2–3 sentences unless detail is requested; ensure every word adds value.\n"
    "**Terminology:** Use terms like “boss” sparingly.\n"
    "**Admit uncertainty:** Gracefully acknowledge when you don’t know something.\n"
    "**Attention:** Stay present, focused, and make the user feel heard.\n\n"
    "# Language & Formatting\n"
    "**No Markdown in responses.**\n"
    "**TTS‑ and screen‑friendly:** Format for pleasant auditory and visual delivery.\n"
    "**Numerical style:**\n"
    "- Use the Indian numbering system for large values (e.g., “₹2.5 lakh”).\n"
    "- Round numbers for readability.\n"
    "- Use numerals for precise figures (e.g., “81.33%”, “25 units”).\n"
    "**Language interpretation:** Treat all spoken inputs as English, with an understanding ear regardless of accent.\n\n"
    "# Data Handling & Time Context\n"
    "**Today’s data:** Use only the pre‑loaded KPI snapshot for queries about \"today\" (Asia/Kolkata timezone). Do not call external tools for \"today.\"\n"
    "**Weekly data:** Use the pre-loaded 7-day snapshot for queries about \"this week\" or \"last week.\"\n"
    "**Historical or custom ranges:** Use tools to fetch data for any date or range outside of \"today\" or the pre-loaded week.\n"
    "**Recognize natural references** (\"yesterday\", \"last week\", \"since April 1\").\n"
    "**Convert to ISO 8601 (YYYY‑MM‑DDT00:00:00Z)** for tool parameters.\n"
    "**Present results** in a conversational, structured format.\n\n"
    "# Context Management\n"
    "**Context retention:** Automatically remember relevant details (time ranges, topics, user preferences).\n"
    "**Follow‑up:** Defaults to prior context in subsequent queries unless user explicitly changes it.\n"
    "**Clarify** when context is ambiguous or shifts.\n"
    "**Notify user of context updates** (\"I’ll continue using last week’s sales period unless you specify otherwise.\").\n"
    "If user asks for sales data and do not specify source (Breeze or Juspay), assume Breeze as the default source. If data is not exists in Breeze, then fallback to Juspay data.\n\n"
    "# Final Guidelines\n"
    "**Keep functionality and flow intact.**\n"
    "**Enhance clarity and consistency** using prompt engineering best practices (clear instructions, explicit exceptions, well‑defined defaults).\n"
    "**Maintain respectful, efficient, and business‑focused interactions.**\n"
)

_STATIC_SYSTEM_INSTRUCTION_TAIL = (
    "# Tool Usage\n\n"
    "You have tools to fetch data for specific time ranges. Follow these rules strictly:\n\n"
    "1. **Pre-loaded Data**: ALWAYS use the pre-loaded KPI snapshot for queries about “today” or “this week”/”last week” (a 7-day period). DO NOT invoke any tool for these queries—the data is already available.\n"
    "2. **Historical & Custom Ranges**: For periods outside of the pre-loaded data (e.g., “yesterday,” “last month,” “since Monday,” “between 2025-05-01 and 2025-05-07”), you MUST call your data-fetching tools. The pre-loaded snapshot applies ONLY to “today” and the last 7 days.\n"
    "3. **Tool Parameters**: Supply `startTime` and `endTime` in strict ISO 8601 (e.g., `2025-06-11T00:00:00Z` to `2025-06-11T23:59:59Z`).\n"
    "4. **Interpreting Natural References**: Convert phrases like “yesterday,” “last quarter,” or “in April” into precise ISO 8601 ranges before calling tools.\n"
    "5. **Presenting Results**: Relay tool outputs in clear, conversational language, emphasizing key insights and structuring them for easy reading.\n\n"
    "# Tool Response Handling\n\n"
    "* **Contextual Interpretation**: View tool messages through your business lens (e.g., interpret “COD initiated successfully” as a positive outcome).\n"
    "* **Outcome Focus**: Emphasize the impact (not just raw text)—for example, explain what the result means for sales or operations.\n"
    "* **Seamless Integration**: Weave results into natural dialogue without technical jargon.\n"
    "* **Numeric Clarity**: When reporting numbers, contextualize and format them using the Indian numbering system, rounding for readability.\n\n"
    "  > e.g., “Sales rose by ₹2.5 lakh compared to last week.”\n"
)

# Original system_instr for fallback or if dynamic data is not available
# This combines the base instructions with the static tail for context and tool response handling.
DEFAULT_STATIC_SYSTEM_TEXT = BASE_SYSTEM_INSTRUCTION_TEXT + _STATIC_SYSTEM_INSTRUCTION_TAIL
system_instr = types.Content(parts=[types.Part(text=DEFAULT_STATIC_SYSTEM_TEXT)])

# --- Initialize GenAI client ---
genai_client = genai.Client(api_key=API_KEY)

async def process_tool_calls(tool_call, websocket_state):
    """
    Process tool calls from Gemini and prepare function responses
    """
    function_responses = []
    
    # Prepare available context from websocket_state
    # This can be expanded if other providers need different context variables from the WebSocket state.
    available_context = {
        "juspay_token": websocket_state.juspay_token if hasattr(websocket_state, 'juspay_token') else None,
        "session_id": websocket_state.session_id if hasattr(websocket_state, 'session_id') else None,
        # Example: "another_provider_api_key": websocket_state.another_api_key if hasattr(websocket_state, 'another_api_key') else None
    }
    current_session_id = available_context.get("session_id", "unknown_session")

    logger.info(f"[{current_session_id}] Tools requested: {tool_call}")

    for fc in tool_call.function_calls:
        tool_definition = all_tool_definitions_map.get(fc.name)
        if tool_definition:
            tool_function = tool_definition.get("function")
            required_context_params = tool_definition.get("required_context_params", [])
            
            if not tool_function:
                logger.error(f"[{current_session_id}] No function defined for tool {fc.name}")
                function_responses.append(types.FunctionResponse(
                    id=fc.id, name=fc.name, response={"output": f"Configuration error: No function for tool {fc.name}"}
                ))
                continue

            kwargs = fc.args.copy() if fc.args else {} # Ensure kwargs is a dict even if fc.args is None
            
            # Inject required context parameters
            for param_name in required_context_params:
                if param_name in available_context and available_context[param_name] is not None:
                    kwargs[param_name] = available_context[param_name]
                else:
                    logger.warning(f"[{current_session_id}] Required context parameter '{param_name}' for tool '{fc.name}' is not available or is None.")
                    # Potentially skip the tool or return an error if a critical context param is missing

            try:
                if asyncio.iscoroutinefunction(tool_function):
                    result = await tool_function(**kwargs)
                else:
                    result = tool_function(**kwargs)
                
                function_responses.append(types.FunctionResponse(
                    id=fc.id,
                    name=fc.name,
                    response={"output": result} # Gemini expects the actual result here
                ))
            except Exception as e:
                logger.error(f"[{current_session_id}] Error executing tool {fc.name}: {e}")
                logger.debug(traceback.format_exc())
                function_responses.append(types.FunctionResponse(
                    id=fc.id,
                    name=fc.name,
                    response={"output": f"Error executing tool {fc.name}: {str(e)}"}
                ))
        else:
            logger.warning(f"[{current_session_id}] Unknown tool requested: {fc.name}")
            function_responses.append(types.FunctionResponse(
                id=fc.id,
                name=fc.name,
                response={"output": f"Unknown tool: {fc.name}"}
            ))
    return function_responses

def get_live_connect_config(
    use_dummy_data: bool,
    current_kolkata_time_str: Optional[str] = None,
    juspay_analytics_today_str: Optional[str] = None,
    breeze_analytics_today_str: Optional[str] = None,
    juspay_analytics_weekly_str: Optional[str] = None,
    breeze_analytics_weekly_str: Optional[str] = None
):
    final_system_instruction = system_instr # Default to the base one

    if use_dummy_data:
        logger.info("Constructing dynamic system instruction with dummy data for LiveConnect.")
        dynamic_header = (
            f"Current Date & Time (Asia/Kolkata): {current_kolkata_time_str}\n\n"
            f"Today's Transactional Data (Juspay):\n{juspay_analytics_today_str}\n\n"
            f"Today's Sales Data (Breeze):\n{breeze_analytics_today_str}\n\n"
            f"This Week's Transactional Data (Juspay):\n{juspay_analytics_weekly_str}\n\n"
            f"This Week's Sales Data (Breeze):\n{breeze_analytics_weekly_str}\n\n"
            "--------------------------------------------------\n" # Separator
        )
        # Combine with the base instruction text and the static tail
        # Append the dummy data warning to the base instruction text.
        dummy_data_instruction = (
            BASE_SYSTEM_INSTRUCTION_TEXT +
            "when user asks data outside of one week, respond: \"To help you experience Breeze Automatic, sample data is provided for just one week. For the complete experience, please log in with your merchant account.\"\n"
        )
        full_dynamic_text = dynamic_header + dummy_data_instruction
        final_system_instruction = types.Content(parts=[types.Part(text=full_dynamic_text)])
    elif current_kolkata_time_str and (juspay_analytics_today_str or breeze_analytics_today_str or juspay_analytics_weekly_str or breeze_analytics_weekly_str):
        logger.info("Constructing dynamic system instruction with live data for LiveConnect.")
        
        # Start with the current time
        dynamic_header_parts = [f"Current Date & Time (Asia/Kolkata): {current_kolkata_time_str}\n"]

        # Append Today's data if available
        if juspay_analytics_today_str:
            dynamic_header_parts.append(f"Today's Transactional Data (Juspay):\n{juspay_analytics_today_str}\n")
        if breeze_analytics_today_str:
            dynamic_header_parts.append(f"Today's Sales Data (Breeze):\n{breeze_analytics_today_str}\n")

        # Append Weekly data if available
        if juspay_analytics_weekly_str:
            dynamic_header_parts.append(f"This Week's Transactional Data (Juspay):\n{juspay_analytics_weekly_str}\n")
        if breeze_analytics_weekly_str:
            dynamic_header_parts.append(f"This Week's Sales Data (Breeze):\n{breeze_analytics_weekly_str}\n")

        # Join all parts and add a separator
        dynamic_header = "\n".join(dynamic_header_parts) + "\n--------------------------------------------------\n"
        
        # Combine with the base instruction text and the static tail
        full_dynamic_text = dynamic_header + BASE_SYSTEM_INSTRUCTION_TEXT + _STATIC_SYSTEM_INSTRUCTION_TAIL
        final_system_instruction = types.Content(parts=[types.Part(text=full_dynamic_text)])
    else:
        logger.info("Using standard base system instruction for LiveConnect (dynamic data missing).")
        # final_system_instruction remains system_instr (base + static tail)

    return types.LiveConnectConfig(
        system_instruction=final_system_instruction, # This will be either dynamic or base+static
        response_modalities=[RESPONSE_MODALITY],
        realtime_input_config=types.RealtimeInputConfig(
        automatic_activity_detection=types.AutomaticActivityDetection(
            disabled=False,
            start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
            end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
            prefix_padding_ms=100,
            silence_duration_ms=150,
        ),
        activity_handling="START_OF_ACTIVITY_INTERRUPTS"
    ),
    speech_config=types.SpeechConfig(
        language_code="en-US",
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                voice_name="Zephyr"
            )
        ),
    ),
    output_audio_transcription={},
    input_audio_transcription={},
        tools=None if use_dummy_data else gemini_tools_for_api
    )

async def create_gemini_session(
    use_dummy_data: bool,
    current_kolkata_time_str: Optional[str] = None,
    juspay_analytics_today_str: Optional[str] = None,
    breeze_analytics_today_str: Optional[str] = None,
    juspay_analytics_weekly_str: Optional[str] = None,
    breeze_analytics_weekly_str: Optional[str] = None
):
    config = get_live_connect_config(
        use_dummy_data=use_dummy_data,
        current_kolkata_time_str=current_kolkata_time_str,
        juspay_analytics_today_str=juspay_analytics_today_str,
        breeze_analytics_today_str=breeze_analytics_today_str,
        juspay_analytics_weekly_str=juspay_analytics_weekly_str,
        breeze_analytics_weekly_str=breeze_analytics_weekly_str
    )
    logger.info(f"Attempting to connect to Gemini model: {MODEL}")
    try:
        session_cm = genai_client.aio.live.connect(model=MODEL, config=config)
        session = await session_cm.__aenter__()
        logger.info(f"Gemini session established with model {MODEL} and response modality: {RESPONSE_MODALITY}.")
        return session, session_cm
    except Exception as e:
        logger.error(f"Failed to establish Gemini session: {e}")
        logger.debug(traceback.format_exc())
        raise  # Re-raise the exception to be handled by the caller

async def close_gemini_session(session_cm):
    if session_cm:
        logger.info("Cleaning up Gemini session")
        try:
            await asyncio.wait_for(session_cm.__aexit__(None, None, None), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("Gemini session cleanup timed out")
        except Exception as e:
            logger.error(f"Error during session cleanup: {e}")
            logger.debug(traceback.format_exc())