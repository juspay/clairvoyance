import json

from pipecat.services.google.stt import GoogleSTTService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.soniox.stt import SonioxInputParams, SonioxSTTService
from pipecat.transcriptions.language import Language

from app.core import config
from app.core.logger import logger
from app.schemas import LeadCallOutcome


def get_stt_service():
    """
    Returns an STT service instance based on the environment configuration.
    """
    if config.BREEZE_BUDDY_STT_SERVICE == "openai":
        logger.info("Using OpenAI STT service for Breeze Buddy voice")
        return OpenAISTTService(
            api_key=config.OPENAI_STT_API_KEY,
            model=config.OPENAI_STT_MODEL,
            language=Language.EN,
            temperature=0.0,
        )
    elif config.BREEZE_BUDDY_STT_SERVICE == "soniox":
        language_hints = None
        if config.BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS:
            lang_list = [
                lang.strip()
                for lang in config.BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS.split(",")
            ]
            language_hints = [Language(lang) for lang in lang_list if lang]

        # Configure Soniox with supported parameters only
        soniox_params = SonioxInputParams(
            model=config.BREEZE_BUDDY_SONIOX_MODEL,
            language_hints=language_hints,
            context=(
                config.BREEZE_BUDDY_SONIOX_CONTEXT
                if config.BREEZE_BUDDY_SONIOX_CONTEXT
                else None
            ),
            enable_non_final_tokens=config.BREEZE_BUDDY_SONIOX_ENABLE_NON_FINAL_TOKENS,
            max_non_final_tokens_duration_ms=(
                config.BREEZE_BUDDY_SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS
                if config.BREEZE_BUDDY_SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS > 0
                else None
            ),
            client_reference_id=None,
        )

        return SonioxSTTService(
            api_key=config.SONIOX_API_KEY,
            params=soniox_params,
            vad_force_turn_endpoint=config.BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT,
        )

    else:
        logger.info("Using Google STT service with VAD-based turn detection")
        return GoogleSTTService(
            params=GoogleSTTService.InputParams(
                languages=[Language.EN_US, Language.EN_IN], enable_interim_results=False
            ),
            credentials=config.GOOGLE_CREDENTIALS_JSON,
        )


# Mapping dictionary for outcome strings to LeadCallOutcome enum values
OUTCOME_TO_ENUM = {
    "confirmed": LeadCallOutcome.CONFIRM,
    "cancelled": LeadCallOutcome.CANCEL,
    "busy": LeadCallOutcome.BUSY,
    "address_updated": LeadCallOutcome.ADDRESS_UPDATED,
    "no_answer": LeadCallOutcome.NO_ANSWER,
    "unknown": LeadCallOutcome.UNKNOWN,
}


def indian_number_to_speech(number: int) -> str:
    if number < 100:
        return f"{number} rupees"

    parts = []
    num_str = str(number)
    n = len(num_str)

    # Process last 3 digits (hundreds)
    if n >= 3:
        last_three = int(num_str[-3:])
        if last_three:
            parts.append(f"{last_three}")

    # Process thousands
    if n > 3:
        thousand = int(num_str[-5:-3]) if n >= 5 else int(num_str[-4:-3])
        if thousand:
            parts.insert(0, f"{thousand} thousand")

    # Process lakhs
    if n > 5:
        lakh = int(num_str[-7:-5]) if n >= 7 else int(num_str[-6:-5])
        if lakh:
            parts.insert(0, f"{lakh} lakh")

    # Process crores
    if n > 7:
        crore = int(num_str[:-7])
        if crore:
            parts.insert(0, f"{crore} crore")

    # Adjust hundreds format for last part
    if parts and int(parts[-1]) >= 100:
        h = int(parts[-1])
        h_part = f"{h // 100} hundred"
        rest = h % 100
        if rest:
            h_part += f" {rest}"
        parts[-1] = h_part

    return " ".join(parts) + " rupees"


def _extract_value_from_input(input_value, expected_key: str = None):
    """Extract value from JSON-like input, dict object, or return the value directly"""
    try:
        # Handle dict objects directly (when LLM passes dict instead of string)
        if isinstance(input_value, dict):
            if expected_key and expected_key in input_value:
                return str(input_value[expected_key]).strip()
            # Otherwise, get the first value
            elif input_value:
                return str(list(input_value.values())[0]).strip()
            else:
                return ""

        # Handle string inputs
        if isinstance(input_value, str):
            # Try to parse as JSON if it looks like JSON
            if input_value.strip().startswith("{") and input_value.strip().endswith(
                "}"
            ):
                try:
                    # Parse the JSON-like string
                    parsed = json.loads(input_value)
                    if isinstance(parsed, dict):
                        # If we have a specific key to look for, use it
                        if expected_key and expected_key in parsed:
                            return str(parsed[expected_key]).strip()
                        # Otherwise, get the first value
                        elif parsed:
                            return str(list(parsed.values())[0]).strip()
                except json.JSONDecodeError:
                    # If JSON parsing fails, return the original stripped value
                    return input_value.strip()

            # If not JSON or parsing failed, return the original value
            return input_value.strip()

        # For any other type, convert to string and strip
        return str(input_value).strip()

    except (AttributeError, TypeError):
        # If anything fails, convert to string and strip
        return (
            str(input_value).strip()
            if hasattr(input_value, "strip")
            else str(input_value)
        )
