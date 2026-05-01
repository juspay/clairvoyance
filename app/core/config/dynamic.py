import json

from app.core.logger import logger
from app.services.live_config.store import get_config

# -----------------------
# Dynamic runtime configs
# -----------------------

# --- Per-provider TTS defaults (Redis-backed, overridable at runtime) ---
# Each provider has a dict of defaults. Template-level TTSConfig fields
# override these; fields left as None in TTSConfig fall back here.

BB_SPEECH_PROVIDER_DEFAULTS: dict[str, dict] = {
    "elevenlabs": {
        "voice_id": "fG9s0SXJb213f4UxVHyG",
        "model": "eleven_flash_v2_5",
        "speed": 1.15,
        "language": "en",
    },
    "cartesia": {
        "voice_id": "bec003e2-3cb3-429c-8468-206a393c67ad",
        "model": "sonic-3",
        "speed": 1.0,
        "volume": 1.0,
        "emotion": "neutral",
        "language": "en",
    },
    "sarvam": {
        "voice_id": "manisha",
        "model": "bulbul:v2",
        "language": "en-IN",
        "speed": 0.9,
        "pitch": 0.0,
    },
}


async def BREEZE_MCP_ENDPOINT_PATH() -> str:
    """Returns BREEZE_MCP_ENDPOINT_PATH from Redis"""
    return await get_config("BREEZE_MCP_ENDPOINT_PATH", "/ai/neurolink", str)


async def ENABLE_BACKGROUND_TASKS() -> bool:
    """Returns ENABLE_BACKGROUND_TASKS from Redis"""
    return await get_config("ENABLE_BACKGROUND_TASKS", "false", bool)


async def DAILY_SUMMARY_HOUR() -> int:
    """Returns DAILY_SUMMARY_HOUR from Redis (24-hour format: 0-23)"""
    return await get_config("DAILY_SUMMARY_HOUR", 21, int)


async def ENABLE_BREEZE_MCP() -> bool:
    """Returns ENABLE_BREEZE_MCP from Redis"""
    return await get_config("ENABLE_BREEZE_MCP", False, bool)


async def ENABLE_CHAT_MODE_PROMPT() -> bool:
    """Returns ENABLE_CHAT_MODE_PROMPT from Redis"""
    return await get_config("ENABLE_CHAT_MODE_PROMPT", True, bool)


# --- Sarvam Configuration ---
async def SARVAM_STT_MODEL() -> str:
    """Returns SARVAM_STT_MODEL from Redis"""
    return await get_config("SARVAM_STT_MODEL", "saarika:v2.5", str)


async def SARVAM_STT_LANGUAGE_CODE() -> str:
    """Returns SARVAM_STT_LANGUAGE_CODE from Redis"""
    return await get_config("SARVAM_STT_LANGUAGE_CODE", "", str)


async def SARVAM_TTS_LANGUAGE_CODE() -> str:
    """Returns SARVAM_TTS_LANGUAGE_CODE from Redis"""
    return await get_config("SARVAM_TTS_LANGUAGE_CODE", "en-IN", str)


async def SARVAM_STT_PROMPT() -> str:
    """Returns SARVAM_STT_PROMPT from Redis"""
    return await get_config("SARVAM_STT_PROMPT", "", str)


async def SARVAM_STT_VAD_SIGNALS() -> bool:
    """Returns SARVAM_STT_VAD_SIGNALS from Redis"""
    return await get_config("SARVAM_STT_VAD_SIGNALS", True, bool)


async def SARVAM_STT_HIGH_VAD_SENSITIVITY() -> bool:
    """Returns SARVAM_STT_HIGH_VAD_SENSITIVITY from Redis"""
    return await get_config("SARVAM_STT_HIGH_VAD_SENSITIVITY", False, bool)


async def SARVAM_TTS_MODEL() -> str:
    """Returns SARVAM_TTS_MODEL from Redis"""
    return await get_config("SARVAM_TTS_MODEL", "bulbul:v2", str)


async def SARVAM_TTS_VOICE_ID() -> str:
    """Returns SARVAM_TTS_VOICE_ID from Redis"""
    return await get_config("SARVAM_TTS_VOICE_ID", "manisha", str)


async def SARVAM_TTS_PITCH() -> float:
    """Returns SARVAM_TTS_PITCH from Redis"""
    return await get_config("SARVAM_TTS_PITCH", 0.0, float)


async def SARVAM_TTS_PACE() -> float:
    """Returns SARVAM_TTS_PACE from Redis"""
    return await get_config("SARVAM_TTS_PACE", 1.0, float)


# --- Breeze Buddy Sarvam STT Configuration ---
async def BB_SARVAM_STT_MODEL() -> str:
    """Returns BB_SARVAM_STT_MODEL from Redis"""
    return await get_config("BB_SARVAM_STT_MODEL", "saarika:v2.5", str)


async def BB_SARVAM_STT_LANGUAGE_CODE() -> str:
    """Returns BB_SARVAM_STT_LANGUAGE_CODE from Redis"""
    return await get_config("BB_SARVAM_STT_LANGUAGE_CODE", "", str)


async def BB_SARVAM_STT_PROMPT() -> str:
    """Returns BB_SARVAM_STT_PROMPT from Redis"""
    return await get_config("BB_SARVAM_STT_PROMPT", "", str)


async def BB_SARVAM_STT_VAD_SIGNALS() -> bool:
    """Returns BB_SARVAM_STT_VAD_SIGNALS from Redis"""
    return await get_config("BB_SARVAM_STT_VAD_SIGNALS", True, bool)


async def BB_SARVAM_STT_HIGH_VAD_SENSITIVITY() -> bool:
    """Returns BB_SARVAM_STT_HIGH_VAD_SENSITIVITY from Redis"""
    return await get_config("BB_SARVAM_STT_HIGH_VAD_SENSITIVITY", False, bool)


async def BB_TTS_SERVICE() -> str:
    """Returns BREEZE_BUDDY_TTS_SERVICE from Redis (default provider name)"""
    return await get_config("BREEZE_BUDDY_TTS_SERVICE", "elevenlabs", str)


async def BB_VOICE_PROVIDER_DEFAULTS(provider: str) -> dict:
    """Returns merged provider defaults: Redis overrides > hardcoded defaults.

    Redis key: BB_VOICE_DEFAULTS_<PROVIDER> (JSON string).
    Falls back to BB_SPEECH_PROVIDER_DEFAULTS[provider] for any missing keys.
    Null values in Redis are treated as "unset" and filtered out.
    """
    hardcoded = BB_SPEECH_PROVIDER_DEFAULTS.get(provider, {})
    redis_key = f"BB_VOICE_DEFAULTS_{provider.upper()}"
    redis_json = await get_config(redis_key, None, str)
    if redis_json:
        try:
            redis_overrides = json.loads(redis_json)
            # Filter out None values — treat them as "unset, use hardcoded default"
            filtered = {k: v for k, v in redis_overrides.items() if v is not None}
            return {**hardcoded, **filtered}
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Failed to parse {redis_key} from Redis: {e}")
    return dict(hardcoded)


async def BB_SARVAM_TTS_ENABLE_PREPROCESSING() -> bool:
    """Returns BB_SARVAM_TTS_ENABLE_PREPROCESSING from Redis"""
    return await get_config("BB_SARVAM_TTS_ENABLE_PREPROCESSING", True, bool)


async def BB_AGGREGATE_SENTENCES(provider: str) -> bool:
    """Returns aggregate_sentences setting for a provider from Redis."""
    key = f"BB_{provider.upper()}_AGGREGATE_SENTENCES"
    return await get_config(key, True, bool)


async def SHOPS_FOR_TEMPLATE_FLOW() -> list[str]:
    """Returns SHOPS_FOR_TEMPLATE_FLOW from Redis as a list of shop identifiers"""
    config_value = await get_config("SHOPS_FOR_TEMPLATE_FLOW", "", str)
    return [shop.strip() for shop in config_value.split(",") if shop.strip()]


# --- Breeze Buddy Azure LLM Configuration ---
async def BREEZE_BUDDY_AZURE_MAX_COMPLETION_TOKENS() -> int:
    """Returns BREEZE_BUDDY_AZURE_MAX_COMPLETION_TOKENS from Redis"""
    return await get_config("BREEZE_BUDDY_AZURE_MAX_COMPLETION_TOKENS", 50, int)


async def BREEZE_BUDDY_AZURE_TEMPERATURE() -> float:
    """Returns BREEZE_BUDDY_AZURE_TEMPERATURE from Redis"""
    return await get_config("BREEZE_BUDDY_AZURE_TEMPERATURE", 0.7, float)


# --- Google Vertex AI Credentials ---
async def GOOGLE_VERTEX_CREDENTIALS_JSON() -> str:
    """Returns GOOGLE_VERTEX_CREDENTIALS_JSON from Redis"""
    return await get_config("GOOGLE_VERTEX_CREDENTIALS_JSON", "", str)


async def GOOGLE_VERTEX_PROJECT_ID() -> str:
    """Returns GOOGLE_VERTEX_PROJECT_ID from Redis"""
    return await get_config("GOOGLE_VERTEX_PROJECT_ID", "breeze-automatic-prod", str)


# --- Daily Mode VAD Configuration (for web/mobile frontends) ---
async def BB_DAILY_VAD_CONFIDENCE() -> float:
    """Returns BB_DAILY_VAD_CONFIDENCE from Redis"""
    return await get_config("BB_DAILY_VAD_CONFIDENCE", 0.9, float)


async def BB_DAILY_VAD_START_SECS() -> float:
    """Returns BB_DAILY_VAD_START_SECS from Redis"""
    return await get_config("BB_DAILY_VAD_START_SECS", 0.25, float)


async def BB_DAILY_VAD_STOP_SECS() -> float:
    """Returns BB_DAILY_VAD_STOP_SECS from Redis"""
    return await get_config("BB_DAILY_VAD_STOP_SECS", 0.95, float)


async def BB_DAILY_VAD_MIN_VOLUME() -> float:
    """Returns BB_DAILY_VAD_MIN_VOLUME from Redis"""
    return await get_config("BB_DAILY_VAD_MIN_VOLUME", 0.75, float)


# --- Telephony Mode VAD Configuration (for Twilio/Plivo/Exotel) ---
# Defaults match the previous static env values (BREEZE_BUDDY_VAD_*) so
# deployments that don't set these Redis keys keep the same behavior.
async def BB_TELEPHONY_VAD_CONFIDENCE() -> float:
    """Returns BB_TELEPHONY_VAD_CONFIDENCE from Redis"""
    return await get_config("BB_TELEPHONY_VAD_CONFIDENCE", 0.5, float)


async def BB_TELEPHONY_VAD_START_SECS() -> float:
    """Returns BB_TELEPHONY_VAD_START_SECS from Redis"""
    return await get_config("BB_TELEPHONY_VAD_START_SECS", 0.1, float)


async def BB_TELEPHONY_VAD_STOP_SECS() -> float:
    """Returns BB_TELEPHONY_VAD_STOP_SECS from Redis"""
    return await get_config("BB_TELEPHONY_VAD_STOP_SECS", 0.3, float)


async def BB_TELEPHONY_VAD_MIN_VOLUME() -> float:
    """Returns BB_TELEPHONY_VAD_MIN_VOLUME from Redis"""
    return await get_config("BB_TELEPHONY_VAD_MIN_VOLUME", 0.4, float)


# --- Langfuse Score Monitoring Configuration ---
async def LANGFUSE_EVALUATORS() -> dict[str, int]:
    """
    Returns LANGFUSE_EVALUATORS from Redis as a dict mapping evaluator name to threshold.
    Format: "evaluator_name:threshold,evaluator_name:threshold"
    Thresholds are on a 1-10 scale. Scores below the threshold trigger alerts.

    Example: "OUTCOME MISMATCH:5,HIGH LATENCY:7" -> {"OUTCOME MISMATCH": 5, "HIGH LATENCY": 7}

    If threshold is not specified for an evaluator, defaults to 5.
    """
    config_value = await get_config("LANGFUSE_EVALUATORS", "", str)
    evaluators = {}
    for item in config_value.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, threshold_str = item.rsplit(":", 1)
            name = name.strip()
            try:
                threshold = int(threshold_str.strip())
            except ValueError:
                threshold = 5  # Default threshold
        else:
            # No threshold specified, use default
            name = item
            threshold = 5
        if name:
            evaluators[name] = threshold
    return evaluators


# --- Noise Cancellation Configuration ---
async def BB_NOISE_CANCELLATION_ENABLED() -> bool:
    """Returns BB_NOISE_CANCELLATION_ENABLED from Redis"""
    return await get_config("BB_NOISE_CANCELLATION_ENABLED", True, bool)


async def BB_NOISE_CANCELLATION_LEVEL() -> int:
    """Returns BB_NOISE_CANCELLATION_LEVEL from Redis (0-100)"""
    return await get_config("BB_NOISE_CANCELLATION_LEVEL", 100, int)


async def BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY() -> bool:
    """Returns BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY from Redis"""
    return await get_config("BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY", True, bool)


# --- Breeze Buddy Transfer Configuration ---
async def BB_TRANSFER_CONFERENCE_TIMEOUT() -> int:
    """Seconds to wait for agent to join conference"""
    return await get_config("BB_TRANSFER_CONFERENCE_TIMEOUT", 30, int)


async def BB_TRANSFER_POLLING_INTERVAL() -> float:
    """Seconds between polling checks"""
    return await get_config("BB_TRANSFER_POLLING_INTERVAL", 2.0, float)


async def BB_TRANSFER_MAX_RETRIES() -> int:
    """Max retries for conference creation"""
    return await get_config("BB_TRANSFER_MAX_RETRIES", 20, int)


async def BB_TRANSFER_RETRY_DELAY() -> float:
    """Seconds between retries"""
    return await get_config("BB_TRANSFER_RETRY_DELAY", 2.0, float)


async def BREEZE_BUDDY_ENABLE_VAD() -> bool:
    """Returns BREEZE_BUDDY_ENABLE_VAD from Redis.

    When False (default), VAD (SileroVADAnalyzer) is disabled for Breeze Buddy agent.
    All VAD-related functionality is gated behind this flag.
    When True, VAD is enabled and used for voice activity detection and turn management.
    """
    return await get_config("BREEZE_BUDDY_ENABLE_VAD", False, bool)


# --- Outbound Rate Limit Configuration ---
async def OUTBOUND_RATE_LIMIT_MAX_CALLS() -> int:
    """Returns OUTBOUND_RATE_LIMIT_MAX_CALLS from Redis"""
    return await get_config("OUTBOUND_RATE_LIMIT_MAX_CALLS", 7, int)


async def OUTBOUND_RATE_LIMIT_WINDOW_SECONDS() -> int:
    """Returns OUTBOUND_RATE_LIMIT_WINDOW_SECONDS from Redis"""
    return await get_config("OUTBOUND_RATE_LIMIT_WINDOW_SECONDS", 3600, int)


async def OUTBOUND_RATE_LIMIT_BLOCK_ENABLED() -> bool:
    """Returns OUTBOUND_RATE_LIMIT_BLOCK_ENABLED from Redis"""
    return await get_config("OUTBOUND_RATE_LIMIT_BLOCK_ENABLED", False, bool)


# --- Service Health Monitoring Configuration ---
async def ENABLE_SERVICE_HEALTH_MONITORING() -> bool:
    """Returns ENABLE_SERVICE_HEALTH_MONITORING from Redis.

    When True, service health monitoring is active and will auto-pause
    calls when upstream service failures exceed thresholds.
    """
    return await get_config("ENABLE_SERVICE_HEALTH_MONITORING", True, bool)


async def SERVICE_HEALTH_AUTO_RESUME_MINUTES() -> int:
    """Returns SERVICE_HEALTH_AUTO_RESUME_MINUTES from Redis.

    Number of minutes with no errors before auto-resuming calls
    after a circuit breaker opens.
    """
    return await get_config("SERVICE_HEALTH_AUTO_RESUME_MINUTES", 15, int)
