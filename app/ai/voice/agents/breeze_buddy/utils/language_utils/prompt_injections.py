"""Language prompt injection utilities for LLM prompts."""

from typing import Dict, List

from app.core.logger import logger


def inject_language_rules(
    role_messages: List[Dict], primary_language: str, language_prompt_enabled: bool
) -> List[Dict]:
    """
    Append language rules to the existing system prompt if language-aware prompting is enabled.

    Args:
        role_messages: List of role message dicts with 'role' and 'content' keys
        primary_language: The primary language to use (e.g., "Telugu", "Hindi")
        language_prompt_enabled: Whether language-aware prompting is enabled

    Returns:
        Modified role_messages list with language rules appended to system prompt.
        Returns original role_messages on any error to ensure the flow continues.
    """
    try:
        if not language_prompt_enabled or primary_language == "English":
            return role_messages

        language_rules = (
            f"\n\n=== LANGUAGE RULES (CRITICAL - READ CAREFULLY) ==="
            f"\nDEFAULT: Speak in {primary_language} throughout the conversation."
            f"\nWHEN TO SWITCH: Only if user clearly speaks a DIFFERENT language (not just English words mixed in) or explicitly asks to switch."
            f"\n- Simple responses like yes, no, haan, ok → Stay in {primary_language}"
            f"\n- English words mixed with {primary_language} → Stay in {primary_language}"
            f"\n- Speak casually but respectfully, don't use too formal or textbook words - speak how people speak in real life in that regional language in India."
            f"\n- Use formal 'you' forms: మీరు (Telugu), आप (Hindi), நீங்கள் (Tamil), ನೀవು (Kannada)."
            f"\n- Use polite, conversational tone - not too casual, not too formal."
            f"\n=== END LANGUAGE RULES ==="
        )

        # Find and modify the first system message to append language rules
        # Only append if system message already exists, otherwise leave as-is
        for i, msg in enumerate(role_messages):
            if msg.get("role") == "system":
                role_messages[i] = {
                    "role": "system",
                    "content": msg.get("content", "") + language_rules,
                }
                logger.info(
                    f"Appended language rules for {primary_language} to existing system prompt"
                )
                return role_messages

        # No system message found, return as-is
        logger.debug(
            f"No system prompt found, skipping language rules injection for {primary_language}"
        )
        return role_messages
    except Exception as e:
        logger.warning(
            f"Error injecting language rules: {e}, returning original messages"
        )
        return role_messages
