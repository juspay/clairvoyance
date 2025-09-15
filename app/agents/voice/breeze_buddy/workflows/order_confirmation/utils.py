from app.schemas import LeadCallOutcome
from app.core.logger import logger
from app.core import config

from pipecat.services.google.stt import GoogleSTTService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.transcriptions.language import Language


def get_stt_service():
    """
    Returns an STT service instance based on the environment configuration.

    Args:
        voice_name: Voice name to determine STT provider override for specific voices
    """
    # Check for MIA voice with OpenAI override
    if config.BREEZE_BUDDY_STT_SERVICE == "openai":
        logger.info("Using OpenAI STT service for Breeze Buddy voice")
        return OpenAISTTService(
            api_key=config.OPENAI_STT_API_KEY,
            model=config.OPENAI_STT_MODEL,
            language=Language.EN,
            temperature=0.0,
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
