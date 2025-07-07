import os
from app.core.logger import logger

# --- Configuration ---

# A helper function to get a required environment variable
def get_required_env(var_name: str) -> str:
    value = os.environ.get(var_name)
    if not value:
        logger.error(f"{var_name} environment variable is required")
        raise ValueError(f"{var_name} environment variable is required")
    return value

# Environment
ENVIRONMENT = os.environ.get("ENVIRONMENT", "production")
PROD_LOG_LEVEL = os.environ.get("PROD_LOG_LEVEL", "INFO")

# Uvicorn
PORT = int(os.environ.get("PORT", 8000))
HOST = os.environ.get("HOST", "0.0.0.0")
UVICORN_RELOAD = os.environ.get("UVICORN_RELOAD", "true").lower() == "true"
UVICORN_LOG_LEVEL = os.environ.get("UVICORN_LOG_LEVEL", "info")

# Gemini Proxy Configuration
GEMINI_API_KEY = get_required_env("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash-live-001")
RESPONSE_MODALITY = os.environ.get("RESPONSE_MODALITY", "AUDIO")

# Pipecat Agent Configuration
DAILY_API_KEY = get_required_env("DAILY_API_KEY")
DAILY_API_URL = os.environ.get("DAILY_API_URL", "https://api.daily.co/v1")
AZURE_OPENAI_API_KEY = get_required_env("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_ENDPOINT = get_required_env("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_MODEL = os.environ.get("AZURE_OPENAI_MODEL", "gpt-4o-automatic")
GOOGLE_CREDENTIALS_JSON = get_required_env("GOOGLE_CREDENTIALS_JSON")
ENABLE_NOISE_REDUCE_FILTER = os.environ.get("ENABLE_NOISE_REDUCE_FILTER", "true").lower() == "true"

# TTS Configuration
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "bQQWtYx9EodAqMdkrNAc") # bQQWtYx9EodAqMdkrNAc
ELEVENLABS_RHEA_VOICE_ID = os.environ.get("ELEVENLABS_RHEA_VOICE_ID", "bQQWtYx9EodAqMdkrNAc")
ELEVENLABS_MODEL_ID = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
GOOGLE_BRET_VOICE = os.environ.get("GOOGLE_BRET_VOICE", "en-IN-Chirp3-HD-Sadaltager")
GOOGLE_MIA_VOICE = os.environ.get("GOOGLE_MIA_VOICE", "en-IN-Chirp3-HD-Despina")

# WebSocket keepalive settings
PING_INTERVAL = int(os.environ.get("WS_PING_INTERVAL", 5))  # seconds
PING_TIMEOUT = int(os.environ.get("WS_PING_TIMEOUT", 10))  # seconds

# Juspay API configuration
GENIUS_API_URL = "https://portal.juspay.in/api/q/query?api-type=genius-query"

# VAD & framing for client-side audio chunking
SAMPLE_RATE = 16000
FRAME_DURATION = 30  # ms
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION / 1000) * 2  # bytes per frame (16-bit PCM)
VAD_CONFIDENCE = float(os.environ.get("VAD_CONFIDENCE", 0.85))
VAD_MIN_VOLUME = float(os.environ.get("VAD_MIN_VOLUME", 0.75))

# Tracing
ENABLE_TRACING = os.environ.get("ENABLE_TRACING", "false").lower() == "true"

# Search
ENABLE_SEARCH_GROUNDING = os.environ.get("ENABLE_SEARCH_GROUNDING", "true").lower() == "true"
GEMINI_SEARCH_RESULT_API_MODEL = os.environ.get("GEMINI_SEARCH_RESULT_API_MODEL", "gemini-2.5-flash-lite-preview-06-17")

logger.info(f"Using Gemini model: {GEMINI_MODEL}")
logger.info(f"Using response modality: {RESPONSE_MODALITY}")
logger.info(f"Tracing enabled: {ENABLE_TRACING}")
logger.info(f"Search grounding enabled: {ENABLE_SEARCH_GROUNDING}")
logger.info(f"Using Gemini search result model: {GEMINI_SEARCH_RESULT_API_MODEL}")