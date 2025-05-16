import os
import logging

logger = logging.getLogger(__name__)

# --- Configuration ---
API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    logger.error("GEMINI_API_KEY environment variable is required")
    raise ValueError("GEMINI_API_KEY environment variable is required")

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash-live-001")
# Default to audio response, but allow override via environment variable
RESPONSE_MODALITY = os.environ.get("RESPONSE_MODALITY", "AUDIO")

# WebSocket keepalive settings
PING_INTERVAL = int(os.environ.get("WS_PING_INTERVAL", 5))  # seconds
PING_TIMEOUT = int(os.environ.get("WS_PING_TIMEOUT", 10))  # seconds

# Juspay API configuration
GENIUS_API_URL = "https://portal.juspay.in/api/q/query?api-type=genius-query"

# VAD & framing for client-side audio chunking
SAMPLE_RATE = 16000
FRAME_DURATION = 30  # ms
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION / 1000) * 2  # bytes per frame (16-bit PCM)

# Feature flag for V2 flow
ENABLE_SIMPLE_V2_FLOW_STR = os.environ.get("ENABLE_SIMPLE_V2_FLOW", "False")
ENABLE_SIMPLE_V2_FLOW = ENABLE_SIMPLE_V2_FLOW_STR.lower() in ('true', '1', 't')

logger.info(f"Using model: {MODEL}")
logger.info(f"Using response modality: {RESPONSE_MODALITY}")
logger.info(f"Enable Simple V2 Flow: {ENABLE_SIMPLE_V2_FLOW}")