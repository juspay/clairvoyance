"""Language detection utilities for multilingual support using LLM."""

import json
from typing import Any, Dict, Optional

import aiohttp

from app.core.config.static import GEMINI_API_KEY
from app.core.logger import logger

# Gemini model for language detection
GEMINI_LANGUAGE_DETECTION_MODEL = "gemini-2.5-flash"

# Supported language codes
SUPPORTED_LANGUAGES = [
    "te-IN",  # Telugu
    "hi-IN",  # Hindi
    "ta-IN",  # Tamil
    "kn-IN",  # Kannada
    "ml-IN",  # Malayalam
    "mr-IN",  # Marathi
    "gu-IN",  # Gujarati
    "bn-IN",  # Bengali
    "pa-IN",  # Punjabi
    "en-IN",  # English (default)
]

# Language code to human-readable name
LANGUAGE_NAMES = {
    "te-IN": "Telugu",
    "hi-IN": "Hindi",
    "ta-IN": "Tamil",
    "kn-IN": "Kannada",
    "ml-IN": "Malayalam",
    "mr-IN": "Marathi",
    "gu-IN": "Gujarati",
    "bn-IN": "Bengali",
    "pa-IN": "Punjabi",
    "en-IN": "English",
}

# Mapping from short codes (LLM might return) to full codes
SHORT_TO_FULL_LANGUAGE_CODE = {
    "te": "te-IN",
    "hi": "hi-IN",
    "ta": "ta-IN",
    "kn": "kn-IN",
    "ml": "ml-IN",
    "mr": "mr-IN",
    "gu": "gu-IN",
    "bn": "bn-IN",
    "pa": "pa-IN",
    "en": "en-IN",
}


async def detect_language_from_payload(payload: Dict[str, Any]) -> Optional[str]:
    """
    Detect the appropriate language code from the lead payload using LLM.

    Analyzes all fields in the payload (customer address, name, shop name, etc.)
    to determine the most appropriate regional language for the customer.

    Args:
        payload: The complete lead payload containing customer details

    Returns:
        Language code (e.g., "te-IN", "hi-IN") or None if detection fails
    """
    if not payload or not GEMINI_API_KEY:
        logger.warning(
            "Cannot detect language: payload is empty or GEMINI_API_KEY not set"
        )
        return None

    # Convert payload to JSON string for the prompt
    payload_str = json.dumps(payload, indent=2, default=str)

    prompt = f"""Analyze this customer lead payload and determine the most appropriate Indian regional language for communication.

Payload:
{payload_str}

Based on the customer's address, location, name, and any other relevant information, determine which language the customer is most likely to speak.

Return ONLY one of these language codes:
- te-IN (Telugu - for Telangana, Andhra Pradesh)
- hi-IN (Hindi - for UP, MP, Bihar, Jharkhand, Rajasthan, Haryana, Uttarakhand, Himachal Pradesh, Chhattisgarh, Delhi, NCR)
- ta-IN (Tamil - for Tamil Nadu)
- kn-IN (Kannada - for Karnataka)
- ml-IN (Malayalam - for Kerala)
- mr-IN (Marathi - for Maharashtra)
- gu-IN (Gujarati - for Gujarat)
- bn-IN (Bengali - for West Bengal)
- pa-IN (Punjabi - for Punjab)
- en-IN (English - default for unknown regions or mixed/unclear locations)

Rules:
1. If the address clearly indicates a specific Indian state, return the corresponding language code
2. If the city/location is identifiable (e.g., Hyderabad, Bangalore, Chennai), map to the appropriate state language
3. If unsure or the location seems international/unclear, return "en-IN"
4. Return ONLY the language code, nothing else

Language code:"""

    try:
        timeout = aiohttp.ClientTimeout(total=10.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_LANGUAGE_DETECTION_MODEL}:generateContent?key={GEMINI_API_KEY}",
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "maxOutputTokens": 200,
                        "temperature": 0,
                    },
                },
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.debug(f"Gemini language detection response: {data}")

                    # Handle different response structures
                    candidates = data.get("candidates", [])
                    if not candidates:
                        logger.warning(f"Gemini returned no candidates: {data}")
                        return None

                    content = candidates[0].get("content", {})
                    parts = content.get("parts", [])

                    if not parts:
                        text = content.get("text", "")
                        if not text:
                            logger.warning(f"Gemini returned empty parts: {data}")
                            return None
                        language_code = text.strip()
                    else:
                        language_code = parts[0].get("text", "").strip()

                    # Normalize and validate the language code
                    # Handle short codes like "te" -> "te-IN"
                    if language_code in SHORT_TO_FULL_LANGUAGE_CODE:
                        language_code = SHORT_TO_FULL_LANGUAGE_CODE[language_code]
                        logger.debug(
                            f"Normalized short language code to: {language_code}"
                        )

                    if language_code in SUPPORTED_LANGUAGES:
                        logger.info(f"LLM detected language: {language_code}")
                        return language_code
                    else:
                        logger.warning(
                            f"LLM returned unsupported language code: {language_code}"
                        )
                        return None
                else:
                    response_text = await response.text()
                    logger.warning(
                        f"Gemini language detection failed: {response.status} - {response_text}"
                    )
                    return None

    except Exception as e:
        logger.warning(f"Gemini language detection error: {e}")
        return None


def get_language_name(language_code: str) -> str:
    """
    Get human-readable language name for a language code.

    Args:
        language_code: Language code (e.g., "te-IN")

    Returns:
        Language name (e.g., "Telugu")
    """
    return LANGUAGE_NAMES.get(language_code, "English")


async def determine_language_for_call(
    template_configurations,
    payload: Dict[str, Any],
    request_id: str = "unknown",
) -> tuple[str, str]:
    """
    Determine the language code and name for a call based on template config and payload.

    This is a unified function that handles language detection with priority:
    1. configurations.stt_language (highest priority - explicit override)
    2. configurations.payload_based_language_selection (LLM detection)
    3. Default to English (en-IN)

    Args:
        template_configurations: Template configurations object (may have stt_language,
                                 payload_based_language_selection attributes)
        payload: The lead/order payload containing customer details
        request_id: Request ID for logging purposes

    Returns:
        Tuple of (language_code, language_name) e.g., ("te-IN", "Telugu")
    """
    language_code = "en-IN"  # Default to English

    if template_configurations:
        if (
            hasattr(template_configurations, "stt_language")
            and template_configurations.stt_language
        ):
            # Highest priority: explicit stt_language setting
            language_code = template_configurations.stt_language
            logger.info(
                f"Using explicit stt_language '{language_code}' for request {request_id}"
            )
        elif (
            hasattr(template_configurations, "payload_based_language_selection")
            and template_configurations.payload_based_language_selection
        ):
            # Second priority: LLM-based language detection
            llm_detected = await detect_language_from_payload(payload)
            if llm_detected:
                language_code = llm_detected
                logger.info(
                    f"LLM detected language '{language_code}' for request {request_id}"
                )
            else:
                logger.info(
                    f"Could not detect language from payload for request {request_id}, using English"
                )
        else:
            logger.info(
                f"Language detection disabled in template for request {request_id}"
            )
    else:
        logger.info(
            f"No configurations in template, using English for request {request_id}"
        )

    return language_code, get_language_name(language_code)
