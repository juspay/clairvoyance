from app.agents.voice.automatic.prompts.system.base import get_base_system_prompt
from app.agents.voice.automatic.prompts.system.charts import (
    get_chart_visualization_instructions,
)
from app.agents.voice.automatic.prompts.system.performance_directives import (
    get_combined_directives,
)
from app.agents.voice.automatic.prompts.system.personalization import append_user_info
from app.agents.voice.automatic.prompts.system.tool_scope import (
    get_tool_scope_instructions,
)
from app.agents.voice.automatic.prompts.system.tts import get_tts_based_instructions
from app.agents.voice.automatic.types import TTSProvider
from app.core.logger import logger


def get_system_prompt(
    user_name: str | None, tts_provider: TTSProvider | None, shop_id: str | None
) -> str:
    """
    Generates a personalized system prompt based on the user's name and TTS service.
    Uses hardcoded prompt (LangFuse integration removed).
    """
    logger.info("Using hardcoded prompt")
    prompt = get_base_system_prompt()
    prompt += get_combined_directives(shop_id)

    # Append dynamic components that are always added locally
    prompt += get_chart_visualization_instructions()
    prompt += get_tts_based_instructions(tts_provider)
    prompt += get_tool_scope_instructions(shop_id)

    if user_name:
        logger.info(f"Personalizing prompt for user: {user_name}")
        prompt += append_user_info(user_name)

    return prompt
