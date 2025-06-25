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

# Gemini Proxy Configuration
GEMINI_API_KEY = get_required_env("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash-live-001")
RESPONSE_MODALITY = os.environ.get("RESPONSE_MODALITY", "AUDIO")

# Pipecat Agent Configuration
DAILY_API_KEY = get_required_env("DAILY_API_KEY")
AZURE_OPENAI_API_KEY = get_required_env("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_ENDPOINT = get_required_env("AZURE_OPENAI_ENDPOINT")
GOOGLE_CREDENTIALS_JSON = get_required_env("GOOGLE_CREDENTIALS_JSON")

# WebSocket keepalive settings
PING_INTERVAL = int(os.environ.get("WS_PING_INTERVAL", 5))  # seconds
PING_TIMEOUT = int(os.environ.get("WS_PING_TIMEOUT", 10))  # seconds

# Juspay API configuration
GENIUS_API_URL = "https://portal.juspay.in/api/q/query?api-type=genius-query"

# VAD & framing for client-side audio chunking
SAMPLE_RATE = 16000
FRAME_DURATION = 30  # ms
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION / 1000) * 2  # bytes per frame (16-bit PCM)

logger.info(f"Using Gemini model: {GEMINI_MODEL}")
logger.info(f"Using response modality: {RESPONSE_MODALITY}")