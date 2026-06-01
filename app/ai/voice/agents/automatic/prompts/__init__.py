from app.ai.voice.agents.automatic.prompts.system.base import get_base_system_prompt
from app.ai.voice.agents.automatic.prompts.system.charts import (
    get_chart_visualization_instructions,
)
from app.ai.voice.agents.automatic.prompts.system.performance_directives import (
    get_combined_directives,
)
from app.ai.voice.agents.automatic.prompts.system.personalization import (
    append_user_info,
)
from app.ai.voice.agents.automatic.prompts.system.tool_scope import (
    get_tool_scope_instructions,
)
from app.ai.voice.agents.automatic.prompts.system.tts import get_tts_based_instructions
from app.ai.voice.agents.automatic.types import TTSProvider
from app.core.config import dynamic
from app.core.logger import logger


async def get_system_prompt(
    user_name: str | None, tts_provider: TTSProvider | None, shop_id: str | None
) -> str:
    """
    Generates a personalized system prompt based on the user's name and TTS service.
    Uses hardcoded prompt (LangFuse integration removed).
    """
    # Check if chat mode prompt is enabled
    if await dynamic.ENABLE_CHAT_MODE_PROMPT():
        logger.info("Using chat mode prompt (ENABLE_CHAT_MODE_PROMPT=true)")
        return """You are Breeze Automatic, a helpful assistant with access to external tools.

                ### The Golden Rule
                **ALWAYS call tools IMMEDIATELY when data is needed. NEVER ask for permission.**
                **ALWAYS call tools IMMEDIATELY when data is needed. NEVER ask for permission.**
                **ALWAYS call tools IMMEDIATELY when data is needed. NEVER ask for permission.**
                **ALWAYS call tools IMMEDIATELY when data is needed. NEVER ask for permission.**
                **ALWAYS call tools IMMEDIATELY when data is needed. NEVER ask for permission.**

                **CURRENT TOKEN LIMIT: 1048576 tokens**
                Your response for this request must fit within 1048576 tokens. Adjust your response detail accordingly.
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
