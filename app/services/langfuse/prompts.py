"""
LangFuse prompt management functions.
Handles fetching and processing prompts from LangFuse.
"""

from typing import Optional

from app.core.config import ENABLE_LANGFUSE_PROMPTS
from app.core.logger import logger
from app.services.langfuse.main import langfuse_client


def fetch_prompt(prompt_name: str, label: str) -> Optional[str]:
    """
    Fetch prompt from LangFuse with error handling.

    Args:
        prompt_name: Name of the prompt (required)
        label: Label for the prompt (required)

    Returns:
        str: Prompt content if successful, None if failed
    """
    if not ENABLE_LANGFUSE_PROMPTS:
        logger.debug("LangFuse prompts not enabled, using fallback")
        return None

    client = langfuse_client.get_client()
    if not client:
        logger.debug("LangFuse client not initialized, using fallback")
        return None

    # Fetch from LangFuse
    try:
        logger.info(
            f"Fetching prompt '{prompt_name}' with label '{label}' from LangFuse"
        )

        # Get prompt from LangFuse
        prompt = client.get_prompt(prompt_name, label=label)

        if prompt and hasattr(prompt, "prompt"):
            prompt_content = prompt.prompt
            logger.info(
                f"Successfully fetched prompt from LangFuse (length: {len(prompt_content)} chars)"
            )
            return prompt_content
        else:
            logger.warning(
                f"Prompt '{prompt_name}' with label '{label}' not found in LangFuse"
            )
            return None

    except Exception as e:
        logger.error(f"Error fetching prompt from LangFuse: {e}")
        return None
