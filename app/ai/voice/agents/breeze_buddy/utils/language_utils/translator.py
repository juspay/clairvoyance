"""Translation utilities for converting transcriptions to English using Gemini LLM."""

import asyncio
import json
from typing import Dict, List, Optional

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.google.llm import GoogleLLMService

from app.core.config.static import (
    GEMINI_API_KEY,
    GEMINI_TRANSLATION_MODEL,
    TRANSLATION_TIMEOUT_SECONDS,
)
from app.core.logger import logger

# Module-level singleton LLM client (avoids creating a new gRPC channel per request)
_translation_llm: GoogleLLMService | None = None
if GEMINI_API_KEY:
    _translation_llm = GoogleLLMService(
        api_key=GEMINI_API_KEY,
        model=GEMINI_TRANSLATION_MODEL,
        params=GoogleLLMService.InputParams(
            max_tokens=8192,
            temperature=0,
            thinking=GoogleLLMService.ThinkingConfig(thinking_budget=0),
            # Force JSON output format to avoid markdown code blocks
            extra={"response_mime_type": "application/json"},
        ),
    )
else:
    logger.warning("GEMINI_API_KEY not set — translation service will be unavailable")


async def translate_transcript_to_english(
    messages: List[Dict[str, str]],
) -> Optional[List[Dict[str, str]]]:
    """
    Translate a list of transcript messages to English using Gemini.

    Args:
        messages: List of {role, content} dicts from the transcription.

    Returns:
        List of {role, content} dicts with content translated to English,
        or None if translation fails.
    """
    if not messages:
        logger.warning("No messages to translate")
        return None

    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY not configured, cannot translate")
        return None

    if not _translation_llm:
        logger.error("Translation LLM service not initialized")
        return None

    # Pre-filter: only keep user/assistant messages with real content.
    # This excludes system prompts, tool responses, and COMPLETED markers
    # that the frontend would strip anyway.
    displayable_roles = {"user", "assistant"}
    filtered_messages = [
        msg
        for msg in messages
        if msg.get("role") in displayable_roles
        and msg.get("content", "").strip() not in ("", "COMPLETED")
    ]

    if not filtered_messages:
        logger.warning("No displayable messages to translate after filtering")
        return None

    logger.info(
        f"Filtered {len(messages)} messages down to {len(filtered_messages)} displayable messages for translation"
    )

    # Build the transcript text for the prompt using filtered messages
    transcript_lines = [
        f"[{i}] ({msg.get('role', 'unknown')}): {msg.get('content', '')}"
        for i, msg in enumerate(filtered_messages)
    ]
    transcript_text = "\n".join(transcript_lines)

    prompt = f"""You are a professional translator. Translate the following conversation transcript to English.

The transcript contains messages between an AI assistant (role: "assistant") and a customer (role: "user").
The messages may be in Hindi, Tamil, Telugu, Kannada, Malayalam, Marathi, Gujarati, Bengali, Punjabi, or any other Indian language.
Some messages may already be in English - keep those as is.

IMPORTANT RULES:
1. Translate ONLY the content text, preserve the exact same message structure
2. Keep proper nouns (names, places, brands) as they are
3. Maintain the tone and intent of the original message
4. If a message is already in English, return it unchanged
5. Return ONLY a valid JSON array, no other text

INPUT TRANSCRIPT:
{transcript_text}

OUTPUT FORMAT - Return a JSON array of objects, each with "role" and "content" keys:
[
  {{"role": "assistant", "content": "translated text here"}},
  {{"role": "user", "content": "translated text here"}}
]

Return ONLY the JSON array, nothing else."""

    try:
        context = LLMContext(messages=[{"role": "user", "content": prompt}])
        response = await asyncio.wait_for(
            _translation_llm.run_inference(context),
            timeout=TRANSLATION_TIMEOUT_SECONDS,
        )

        if not response:
            logger.error("Empty response from Gemini translation")
            return None

        # Parse the JSON response. Since we set response_mime_type="application/json",
        # the response should already be valid JSON (no markdown code blocks).
        translated = _parse_translation_response(response, len(filtered_messages))
        if not translated:
            logger.error("Failed to parse translation response")
            return None

        # Ensure role fields match the filtered (displayable) messages.
        # We trust the order from Gemini but restore correct roles from source.
        for i, msg in enumerate(translated):
            if i < len(filtered_messages):
                msg["role"] = filtered_messages[i]["role"]

        logger.info(f"Successfully translated {len(translated)} messages to English")
        return translated

    except asyncio.TimeoutError:
        logger.error(
            f"Translation timed out after {TRANSLATION_TIMEOUT_SECONDS}s "
            f"for {len(filtered_messages)} messages"
        )
        return None
    except Exception as e:
        logger.error(f"Error translating transcript: {e}", exc_info=True)
        return None


def _parse_translation_response(
    response: str, expected_count: int
) -> Optional[List[Dict[str, str]]]:
    """
    Parse the JSON translation response from Gemini.

    Args:
        response: The raw response string (expected to be valid JSON due to response_mime_type).
        expected_count: Expected number of messages in the translation.

    Returns:
        List of validated {role, content} dicts, or None if parsing/validation fails.
    """
    try:
        parsed = json.loads(response)

        if not isinstance(parsed, list):
            logger.error(f"Translation response is not a list: {type(parsed)}")
            return None

        # Validate each message has required fields
        validated = []
        for item in parsed:
            if isinstance(item, dict) and "role" in item and "content" in item:
                validated.append(
                    {"role": str(item["role"]), "content": str(item["content"])}
                )
            else:
                logger.warning(f"Skipping invalid translation item: {item}")

        # Fail fast on count mismatch - don't cache partial translations
        if len(validated) != expected_count:
            logger.error(
                f"Translation count mismatch: got {len(validated)}, expected {expected_count}. "
                f"Rejecting translation to prevent partial data."
            )
            return None

        if not validated:
            logger.error("No valid translated messages found")
            return None

        return validated

    except json.JSONDecodeError as e:
        # Log only metadata, not response content (may contain PII)
        logger.error(
            f"Failed to parse translation JSON: {e} | "
            f"model={GEMINI_TRANSLATION_MODEL} | "
            f"response_length={len(response)}"
        )
        return None
    except Exception as e:
        logger.error(f"Error parsing translation response: {e}")
        return None
