import os
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

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
APP_BASE_URL = os.environ.get("APP_BASE_URL", "")

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
ENABLE_NOISE_REDUCE_FILTER = (
    os.environ.get("ENABLE_NOISE_REDUCE_FILTER", "true").lower() == "true"
)
ENABLE_AIC_FILTER = os.environ.get("ENABLE_AIC_FILTER", "false").lower() == "true"
AICOUSTICS_LICENSE_KEY = os.environ.get("AICOUSTICS_LICENSE_KEY", "")

# AIC Filter Parameters (simplified for tuning)
AIC_ENHANCEMENT_LEVEL = float(os.environ.get("AIC_ENHANCEMENT_LEVEL", "1.0"))
AIC_VOICE_GAIN = float(os.environ.get("AIC_VOICE_GAIN", "1.2"))
AIC_NOISE_GATE_ENABLE = (
    os.environ.get("AIC_NOISE_GATE_ENABLE", "true").lower() == "true"
)

# TTS Configuration
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.environ.get(
    "ELEVENLABS_VOICE_ID", "bQQWtYx9EodAqMdkrNAc"
)  # bQQWtYx9EodAqMdkrNAc
ELEVENLABS_RHEA_VOICE_ID = os.environ.get(
    "ELEVENLABS_RHEA_VOICE_ID", "bQQWtYx9EodAqMdkrNAc"
)
ELEVENLABS_MODEL_ID = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
ELEVENLABS_VOICE_SPEED = float(os.environ.get("ELEVENLABS_VOICE_SPEED", 1.15))
ELEVENLABS_TTS_SPEED = float(os.environ.get("ELEVENLABS_TTS_SPEED", "1.10"))
ELEVENLABS_BB_VOICE_ID = os.environ.get(
    "ELEVENLABS_BB_VOICE_ID", "fG9s0SXJb213f4UxVHyG"
)
GOOGLE_BRET_VOICE = os.environ.get("GOOGLE_BRET_VOICE", "en-IN-Chirp3-HD-Sadaltager")
GOOGLE_MIA_VOICE = os.environ.get("GOOGLE_MIA_VOICE", "en-IN-Chirp3-HD-Despina")

# WebSocket keepalive settings
PING_INTERVAL = int(os.environ.get("WS_PING_INTERVAL", 5))  # seconds
PING_TIMEOUT = int(os.environ.get("WS_PING_TIMEOUT", 10))  # seconds

# Juspay API configuration
GENIUS_API_URL = "https://portal.juspay.in/api/q/query?api-type=genius-query"
EULER_DASHBOARD_API_URL = os.environ.get(
    "EULER_DASHBOARD_API_URL", "https://portal.juspay.in"
)

# VAD & framing for client-side audio chunking
SAMPLE_RATE = 16000
FRAME_DURATION = 30  # ms
FRAME_SIZE = (
    int(SAMPLE_RATE * FRAME_DURATION / 1000) * 2
)  # bytes per frame (16-bit PCM)
VAD_CONFIDENCE = float(os.environ.get("VAD_CONFIDENCE", 0.85))
VAD_MIN_VOLUME = float(os.environ.get("VAD_MIN_VOLUME", 0.75))
VAD_START_SECS = float(os.environ.get("VAD_START_SECS", 0.30))
VAD_STOP_SECS = float(os.environ.get("VAD_STOP_SECS", 1.00))

# Mem0 Configuration
MEM0_API_KEY = os.getenv("MEM0_API_KEY", "")
MEM0_ENABLED = os.getenv("MEM0_ENABLED", "false").lower() == "true"
MEM0_MAX_FAILURES = int(os.getenv("MEM0_MAX_FAILURES", "3"))
MEM0_RETRY_INTERVAL = int(os.getenv("MEM0_RETRY_INTERVAL", "300"))
MEM0_SESSION_TIMEOUT = int(os.getenv("MEM0_SESSION_TIMEOUT", "3600"))
MEM0_MIN_MESSAGE_LENGTH = int(os.getenv("MEM0_MIN_MESSAGE_LENGTH", "10"))

# Tracing
ENABLE_TRACING = os.environ.get("ENABLE_TRACING", "false").lower() == "true"
OPEN_OBSERVE_BASE_URL = os.environ.get(
    "OPEN_OBSERVE_BASE_URL", "https://periscope.breeze.in"
)

# Text sanitization
SANITIZE_TEXT_FOR_TTS = (
    os.environ.get("SANITIZE_TEXT_FOR_TTS", "false").lower() == "true"
)

# Audio recording
ENABLE_AUTOMATIC_DAILY_RECORDING = (
    os.environ.get("ENABLE_AUTOMATIC_DAILY_RECORDING", "false").lower() == "true"
)

# Search
ENABLE_SEARCH_GROUNDING = (
    os.environ.get("ENABLE_SEARCH_GROUNDING", "true").lower() == "true"
)
GEMINI_SEARCH_RESULT_API_MODEL = os.environ.get(
    "GEMINI_SEARCH_RESULT_API_MODEL", "gemini-2.5-flash-lite-preview-06-17"
)

# --- STT Configuration ---
STT_PROVIDER = os.environ.get(
    "STT_PROVIDER", "google"
).lower()  # "google", "assemblyai", or "openai"
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
OPENAI_STT_API_KEY = os.getenv("OPENAI_STT_API_KEY")
OPENAI_STT_MODEL = os.environ.get(
    "OPENAI_STT_MODEL", "gpt-4o-transcribe"
)  # or "whisper-1"
ENFORCED_OPENAI_STT_MODEL = os.environ.get("ENFORCED_OPENAI_STT_MODEL", "whisper-1")
ENABLE_OPENAI_FOR_MIA = (
    os.environ.get("ENABLE_OPENAI_FOR_MIA", "false").lower() == "true"
)

logger.info(f"Using Gemini model: {GEMINI_MODEL}")
logger.info(f"Using response modality: {RESPONSE_MODALITY}")
logger.info(f"Tracing enabled: {ENABLE_TRACING}")
logger.info(f"Search grounding enabled: {ENABLE_SEARCH_GROUNDING}")
logger.info(f"Using Gemini search result model: {GEMINI_SEARCH_RESULT_API_MODEL}")

# Automatic MCP Tool Server
AUTOMATIC_MCP_TOOL_SERVER_USAGE = (
    os.environ.get("AUTOMATIC_MCP_TOOL_SERVER_USAGE", "false").lower() == "true"
)
AUTOMATIC_TOOL_MCP_SERVER_URL = os.environ.get(
    "AUTOMATIC_TOOL_MCP_SERVER_URL", "https://portal.breeze.in/ai/mcp"
)
MCP_CLIENT_TIMEOUT = int(os.environ.get("MCP_CLIENT_TIMEOUT", 30))  # seconds

_shops_for_mcp_str = os.environ.get("SHOPS_FOR_AUTOMATIC_MCP_SERVER", "")
SHOPS_FOR_AUTOMATIC_MCP_SERVER = [
    shop.strip() for shop in _shops_for_mcp_str.split(",") if shop.strip()
]

# Selective MCP Functions (used when AUTOMATIC_MCP_TOOL_SERVER_USAGE is true)
_selective_mcp_functions_str = os.environ.get("SELECTIVE_MCP_FUNCTIONS", "")
SELECTIVE_MCP_FUNCTIONS = [
    func.strip() for func in _selective_mcp_functions_str.split(",") if func.strip()
]

logger.info(f"Shops enabled for Automatic MCP Server: {SHOPS_FOR_AUTOMATIC_MCP_SERVER}")
logger.info(f"Selective MCP functions enabled: {SELECTIVE_MCP_FUNCTIONS}")

LIGHTHOUSE_APP_URL = os.environ.get("LIGHTHOUSE_APP_URL", "http://localhost:5173")
ENABLE_ALL_METRICS_FROM_CKH = (
    os.environ.get("ENABLE_ALL_METRICS_FROM_CKH", "true").lower() == "true"
)

# Get authorized users from environment, split and normalize
AUTOMATIC_WRITE_ACTIONS_AUTHORIZED_USERS = [
    email.strip().lower()
    for email in os.environ.get("AUTOMATIC_WRITE_ACTIONS_AUTHORIZED_USERS", "").split(
        ","
    )
    if email.strip()
]

# Get write actions from environment, split and normalize
AUTOMATIC_ACTIONS_REQUIRE_AUTH = [
    action.strip().lower()
    for action in os.environ.get("AUTOMATIC_ACTIONS_REQUIRE_AUTH", "").split(",")
    if action.strip()
]

# Context Summarization Configuration
ENABLE_SUMMARIZATION = os.environ.get("ENABLE_SUMMARIZATION", "true").lower() == "true"
MAX_TURNS_BEFORE_SUMMARY = int(os.environ.get("MAX_TURNS_BEFORE_SUMMARY", 10))
KEEP_RECENT_TURNS = int(os.environ.get("KEEP_RECENT_TURNS", 2))

AZURE_BREEZE_BUDDY_OPENAI_MODEL = os.environ.get(
    "AZURE_BREEZE_BUDDY_OPENAI_MODEL", "gpt-4o-automatic"
)

# Twilio settings
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WEBSOCKET_URL = os.getenv("TWILIO_WEBSOCKET_URL", "")
# Webhook Authentication
ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY = os.getenv(
    "ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY", ""
)
ORDER_CONFIRMATION_TOKEN = os.getenv("ORDER_CONFIRMATION_TOKEN", "")

# PostgreSQL Database Configuration
POSTGRES_USER = os.getenv("POSTGRES_USER", "")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "")
POSTGRES_DB = os.getenv("POSTGRES_DB", "")

# Connection pool settings
POSTGRES_POOL_SIZE = int(os.getenv("POSTGRES_POOL_SIZE", "5"))
POSTGRES_MAX_OVERFLOW = int(os.getenv("POSTGRES_MAX_OVERFLOW", "10"))
POSTGRES_POOL_RECYCLE = int(os.getenv("POSTGRES_POOL_RECYCLE", "3600"))  # 1 hour

# JWT Authentication Configuration
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "")
JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(
    os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "60")
)

BREEZE_BUDDY_VAD_CONFIDENCE = os.getenv(
    "BREEZE_BUDDY_VAD_CONFIDENCE", 0.7
)  # Require stronger confidence
BREEZE_BUDDY_VAD_START_SECS = os.getenv(
    "BREEZE_BUDDY_VAD_START_SECS", 0.2
)  # Pick up quicker
BREEZE_BUDDY_VAD_STOP_SECS = os.getenv(
    "BREEZE_BUDDY_VAD_STOP_SECS", 0.8
)  # Allow small pauses
BREEZE_BUDDY_VAD_MIN_VOLUME = os.getenv(
    "BREEZE_BUDDY_VAD_MIN_VOLUME", 0.6
)  # More tolerant for soft voice
BREEZE_BUDDY_STT_SERVICE = os.getenv(
    "BREEZE_BUDDY_STT_SERVICE", "google"
).lower()  # "google" or "openai"

# Session inactivity timeout
AUTOMATIC_SESSION_INACTIVITY_TIMEOUT = float(
    os.environ.get("AUTOMATIC_SESSION_INACTIVITY_TIMEOUT", 900.0)
)
MAX_DAILY_SESSION_LIMIT = int(os.environ.get("MAX_DAILY_SESSION_LIMIT", 1800))

# Human-in-the-Loop (HITL) Configuration
HITL_ENABLE = os.environ.get("HITL_ENABLE", "true").lower() == "true"
FUNCTION_CONFIRMATION_TIMEOUT = int(
    os.environ.get("FUNCTION_CONFIRMATION_TIMEOUT", "30")
)

# HITL Actions Configuration
_hitl_actions_str = os.environ.get("HITL_ACTIONS", "delete")
HITL_ACTIONS = [
    action.strip().lower() for action in _hitl_actions_str.split(",") if action.strip()
]

# Chart Generation Configuration
ENABLE_CHARTS = os.environ.get("ENABLE_CHARTS", "false").lower() == "true"

# PTT VAD Filter Configuration
DISABLE_VAD_FOR_PTT = os.environ.get("DISABLE_VAD_FOR_PTT", "true").lower() == "true"

BREEZE_DEFAULT_SALES_TAB = os.environ.get("BREEZE_DEFAULT_SALES_TAB", "SALES")

# Breeze Portal URLs
AWS_BREEZE_PORTAL_URL = os.environ.get(
    "AWS_BREEZE_PORTAL_URL", "https://portal.breeze.in"
)
GCP_BREEZE_PORTAL_URL = os.environ.get(
    "GCP_BREEZE_PORTAL_URL", "https://portal.breezesdk.store"
)
AUTOMATIC_OPENAI_STT_PROMPT = os.environ.get("AUTOMATIC_OPENAI_STT_PROMPT", "")

EXOTEL_ACCOUNT_SID = os.getenv("EXOTEL_ACCOUNT_SID", "")
EXOTEL_API_KEY = os.getenv("EXOTEL_API_KEY", "")
EXOTEL_API_TOKEN = os.getenv("EXOTEL_API_TOKEN", "")
EXOTEL_SUBDOMAIN = os.getenv("EXOTEL_SUBDOMAIN", "api.exotel.com")
EXOTEL_APPLET_APP_ID = os.getenv("EXOTEL_APPLET_APP_ID", "1044183")


# LangFuse Configuration
ENABLE_LANGFUSE_PROMPTS = (
    os.environ.get("ENABLE_LANGFUSE_PROMPTS", "false").lower() == "true"
)
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "")
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_BASEURL = os.environ.get("LANGFUSE_BASEURL", "https://us.cloud.langfuse.com")
AUTOMATIC_LANGFUSE_PROMPT_NAME = os.environ.get(
    "AUTOMATIC_LANGFUSE_PROMPT_NAME", "AUTOMATIC_VOICE_LANGFUSE_PROMPT"
)
AUTOMATIC_LANGFUSE_SYSTEM_PROMPT_LABEL = os.environ.get(
    "AUTOMATIC_LANGFUSE_SYSTEM_PROMPT_LABEL", "automatic_system_langfuse_prompt"
)

logger.info(f"LangFuse prompts enabled: {ENABLE_LANGFUSE_PROMPTS}")
if ENABLE_LANGFUSE_PROMPTS:
    logger.info(
        f"LangFuse system prompt: {AUTOMATIC_LANGFUSE_PROMPT_NAME} (label: {AUTOMATIC_LANGFUSE_SYSTEM_PROMPT_LABEL})"
    )
