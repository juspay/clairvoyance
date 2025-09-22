from app.agents.voice.automatic.prompts.system.base import get_base_system_prompt
from app.agents.voice.automatic.prompts.system.charts import (
    get_chart_visualization_instructions,
)
from app.agents.voice.automatic.prompts.system.personalization import append_user_info
from app.agents.voice.automatic.prompts.system.tool_scope import (
    get_tool_scope_instrucations,
)
from app.agents.voice.automatic.prompts.system.tts import get_tts_based_instructions
from app.agents.voice.automatic.prompts.system.utils import (
    process_langfuse_template_variables,
)
from app.agents.voice.automatic.types import TTSProvider
from app.core.config import (
    AUTOMATIC_LANGFUSE_PROMPT_NAME,
    AUTOMATIC_LANGFUSE_SYSTEM_PROMPT_LABEL,
    ENABLE_LANGFUSE_PROMPTS,
)
from app.core.logger import logger
from app.services.langfuse.prompts import fetch_prompt


def get_system_prompt(user_name: str | None, tts_provider: TTSProvider | None) -> str:
    """
    Generates a personalized system prompt based on the user's name and TTS service.
    First attempts to fetch from LangFuse, then falls back to hardcoded prompt.
    """
    langfuse_prompt = None

    # Only try to fetch prompt from LangFuse if it's enabled
    if ENABLE_LANGFUSE_PROMPTS:
        langfuse_prompt = fetch_prompt(
            prompt_name=AUTOMATIC_LANGFUSE_PROMPT_NAME,
            label=AUTOMATIC_LANGFUSE_SYSTEM_PROMPT_LABEL,
        )

    if langfuse_prompt:
        logger.info("Using dynamic prompt from LangFuse")
        prompt = process_langfuse_template_variables(langfuse_prompt)
    else:
        logger.info("Using fallback hardcoded prompt")
        prompt = get_base_system_prompt()

    # Append dynamic components that are always added locally
    prompt += get_chart_visualization_instructions()
    prompt += get_tts_based_instructions(tts_provider)
    prompt += get_tool_scope_instrucations()

    if user_name:
        logger.info(f"Personalizing prompt for user: {user_name}")
        prompt += append_user_info(user_name)

    return prompt
