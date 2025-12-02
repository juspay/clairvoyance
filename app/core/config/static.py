import os

from loguru import logger

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

# Pipecat Agent Configuration
AUTOMATIC_CONNECT_BLOCKED_ORIGINS = [
    item.strip()
    for item in os.environ.get("AUTOMATIC_CONNECT_BLOCKED_ORIGINS", "").split(",")
    if item.strip()
]
DAILY_API_KEY = get_required_env("DAILY_API_KEY")
DAILY_API_URL = os.environ.get("DAILY_API_URL", "https://api.daily.co/v1")
AZURE_OPENAI_API_KEY = get_required_env("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_ENDPOINT = get_required_env("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_MODEL = os.environ.get("AZURE_OPENAI_MODEL", "gpt-4o-automatic")
GOOGLE_CREDENTIALS_JSON = get_required_env("GOOGLE_CREDENTIALS_JSON")

# GCS Configuration
GCS_CREDENTIALS_JSON = os.environ.get("GCS_CREDENTIALS_JSON", "")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "atoms-sdk")

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

# Krisp Audio Filter Configuration
ENABLE_KRISP_FILTER = os.environ.get("ENABLE_KRISP_FILTER", "false").lower() == "true"
KRISP_MODEL_PATH = os.environ.get(
    "KRISP_MODEL_PATH", "/app/models/voice/krisp/krisp-viva-tel-v2.kef"
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

# Tool Call Sound Configuration
ENABLE_TOOL_CALL_SOUND = (
    os.environ.get("ENABLE_TOOL_CALL_SOUND", "false").lower() == "true"
)
TOOL_CALL_SOUND_FILE = os.environ.get(
    "TOOL_CALL_SOUND_FILE", "assets/sounds/think2.wav"
)

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
DISABLE_SILERO_VAD = (
    os.environ.get("DISABLE_SILERO_VAD", "false").lower() == "true"
)  # Disable Silero VAD (use when STT provider has built-in VAD)

ENABLE_MUTE_UNTIL_FIRST_BOT_COMPLETE = (
    os.environ.get("ENABLE_MUTE_UNTIL_FIRST_BOT_COMPLETE", "false").lower() == "true"
)

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
).lower()  # "google", "assemblyai", "openai", "deepgram", or "soniox"
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
OPENAI_STT_API_KEY = os.getenv("OPENAI_STT_API_KEY")
OPENAI_STT_MODEL = os.environ.get(
    "OPENAI_STT_MODEL", "gpt-4o-transcribe"
)  # or "whisper-1"
ENFORCED_OPENAI_STT_MODEL = os.environ.get("ENFORCED_OPENAI_STT_MODEL", "whisper-1")
ENABLE_OPENAI_FOR_MIA = (
    os.environ.get("ENABLE_OPENAI_FOR_MIA", "false").lower() == "true"
)

# --- Deepgram STT Configuration ---
DEEPGRAM_API_KEY = os.getenv(
    "DEEPGRAM_API_KEY"
)  # Required API key for Deepgram authentication
DEEPGRAM_MODEL = os.environ.get(
    "DEEPGRAM_MODEL", "nova-3-general"
)  # Deepgram model (nova-3-general recommended for balanced accuracy/speed)
DEEPGRAM_LANGUAGE = os.environ.get(
    "DEEPGRAM_LANGUAGE", "en"
)  # Language code for transcription (en, en-US, en-IN, etc.)
DEEPGRAM_ENDPOINTING = (
    os.environ.get("DEEPGRAM_ENDPOINTING", "true").lower() == "true"
)  # Enable smart endpointing for automatic turn detection
DEEPGRAM_VAD_EVENTS = (
    os.environ.get("DEEPGRAM_VAD_EVENTS", "true").lower() == "true"
)  # Enable Voice Activity Detection events (SpeechStarted/UtteranceEnd)
DEEPGRAM_UTTERANCE_END_MS = int(
    os.environ.get("DEEPGRAM_UTTERANCE_END_MS", "1000")
)  # Milliseconds to wait before considering utterance ended
DEEPGRAM_NO_DELAY = (
    os.environ.get("DEEPGRAM_NO_DELAY", "true").lower() == "true"
)  # Enable real-time processing with minimal delay
DEEPGRAM_SMART_FORMAT = (
    os.environ.get("DEEPGRAM_SMART_FORMAT", "true").lower() == "true"
)  # Apply smart formatting (phone numbers, dates, currency)
DEEPGRAM_PUNCTUATE = (
    os.environ.get("DEEPGRAM_PUNCTUATE", "true").lower() == "true"
)  # Add punctuation to transcription for readability
DEEPGRAM_NUMERALS = (
    os.environ.get("DEEPGRAM_NUMERALS", "true").lower() == "true"
)  # Convert spoken numbers to numerals (critical for Indian lakhs/crores)
DEEPGRAM_PROFANITY_FILTER = (
    os.environ.get("DEEPGRAM_PROFANITY_FILTER", "false").lower() == "true"
)  # Filter profanity (disabled for business context)
DEEPGRAM_DIARIZE = (
    os.environ.get("DEEPGRAM_DIARIZE", "false").lower() == "true"
)  # Enable speaker diarization (disabled for single-speaker voice agent)
# Language detection options (streaming API only supports 'multi' for auto-detection or single language)
DEEPGRAM_AUTO_DETECT_LANGUAGE = (
    os.environ.get("DEEPGRAM_AUTO_DETECT_LANGUAGE", "false").lower() == "true"
)  # Enable automatic language detection (uses 'multi' parameter)

# --- Soniox STT Configuration ---
# Soniox is optimized to solve the 0.5-second speech pause issue experienced with Deepgram
SONIOX_API_KEY = os.getenv(
    "SONIOX_API_KEY"
)  # Required API key for Soniox authentication
SONIOX_MODEL = os.environ.get(
    "SONIOX_MODEL", "stt-rt-preview"
)  # Soniox model optimized for real-time conversation
SONIOX_LANGUAGE_HINTS = os.environ.get(
    "SONIOX_LANGUAGE_HINTS", "en"
)  # Language hints for transcription (comma-separated: en,hi,es)
SONIOX_CONTEXT = os.environ.get(
    "SONIOX_CONTEXT",
    '{ "general": [ {"key": "organisation", "value": "Juspay"}, {"key": "company", "value": "Breeze"}, {"key": "product", "value": "Breeze Automatic"}, {"key": "related_product", "value": "Breeze Checkout"}, {"key": "domain", "value": "D2C ecommerce analytics and business intelligence"}, {"key": "service_type", "value": "AI chatbot for merchant data analysis"}, {"key": "data_sources", "value": "Shopify, payment gateways, Meta Ads, Google Ads, Google Analytics"}, {"key": "user", "value": "D2C merchant, brand owner, or marketing manager"}, {"key": "conversation_type", "value": "performance analysis, metrics review, optimization insights"} ], "text": "User Persona: D2C ecommerce merchants and marketing managers running Shopify stores who need quick, actionable insights from their business data. They are results-focused, time-constrained, and prefer asking natural questions over building complex reports. They monitor key metrics like revenue, conversion rates, CAC, and ROAS daily, making rapid decisions about budget allocation and optimization strategies.Breeze Automatic is an AI-powered analytics conversational chatbot designed specifically for direct-to-consumer ecommerce merchants. It collates and analyzes data from multiple sources including Shopify stores, payment gateways, Meta advertising platforms, Google Ads campaigns, and Google Analytics to provide comprehensive business insights. Merchants use Breeze Automatic to understand their checkout performance, advertising return on investment, conversion funnel optimization, and overall business health. Common conversational patterns include asking about daily or weekly revenue performance, comparing current metrics to previous time periods like month-over-month or year-over-year, investigating drops in conversion rates or spikes in customer acquisition costs, analyzing which advertising channels are performing best, understanding customer behavior and segmentation, tracking checkout abandonment rates, evaluating the effectiveness of marketing campaigns across Meta and Google platforms, and identifying opportunities to improve profitability. Merchants often inquire about their top-performing products, customer lifetime value trends, retention metrics, and how their ad spend efficiency compares across different channels. The chatbot helps answer questions like how todays sales compare to yesterday, which ad campaigns are driving the highest return on ad spend, why checkout conversion might be declining, what the blended customer acquisition cost is across all channels, and how to allocate budget between Meta Ads and Google Ads for maximum return. Users discuss payment success rates, failed transactions, refund patterns, and fraud indicators. They ask about traffic sources, landing page performance, and which marketing touchpoints contribute most to conversions. Breeze Automatic enables merchants to make data-driven decisions by translating complex analytics into actionable insights through natural conversation.", "terms": [ "Juspay", "Breeze Automatic", "Breeze Checkout", "Shopify", "Razorpay", "Cashfree", "PayU", "Easebuzz", "Meta Ads", "Google Ads", "Google Analytics", "D2C", "PSR", "GMV", "UPI", "ROAS", "AOV", "RTO", "COD", "CAC", "LTV", "CPC", "CPM", "CTR", "NDR", "AWB", "SKU", "Sales", "Cart", "Abandonment", "Split", "Yesterday", "Dispatches", "Orders", "Fulfillment", "Processing", "Shipped", "Delivered", "In-transit", "Return", "Exchange", "Refund", "Settlement", "Payout", "Transaction", "Failed payment", "Payment pending", "Chargeback", "Disputed transaction", "Payment success rate", "Non-delivery report", "Undelivered", "Pin code", "Serviceable", "New customer", "Returning customer", "Repeat purchase", "Customer cohort", "Customer segment", "First-time buyer", "Repeat buyer", "Campaign", "Ad set", "Creative", "Cost per click", "Cost per mille", "Click-through rate", "Impression", "Reach", "Engagement", "Conversion", "Pixel", "Attribution", "Conversion rate", "Bounce rate", "Sessions", "Pageviews", "Unique visitors", "Add-to-cart rate", "Checkout drop-off", "blended CAC", "checkout abandonment", "conversion funnel", "ad spend", "Google Ads Spend", "customer acquisition cost", "return on ad spend", "month-over-month", "year-over-year", "payment gateway", "customer lifetime value", "marketing touchpoints", "landing page performance", "traffic sources", "checkout conversion", "refund patterns", "fraud indicators", "retention metrics", "budget allocation", "ecommerce", "direct-to-consumer", "Out of stock", "Inventory turnover" ], "translation_terms": [ {"source": "Juspay", "target": "Juspay"}, {"source": "Breeze", "target": "Breeze"}, {"source": "Shopify", "target": "Shopify"}, {"source": "Razorpay", "target": "Razorpay"}, {"source": "Cashfree", "target": "Cashfree"}, {"source": "PayU", "target": "PayU"}, {"source": "Easebuzz", "target": "Easebuzz"}, {"source": "Meta Ads", "target": "Meta Ads"}, {"source": "Google Ads", "target": "Google Ads"}, {"source": "ROAS", "target": "ROAS"}, {"source": "CAC", "target": "CAC"}, {"source": "D2C", "target": "D2C"}, {"source": "PSR", "target": "PSR"}, {"source": "GMV", "target": "GMV"}, {"source": "UPI", "target": "UPI"}, {"source": "AOV", "target": "AOV"}, {"source": "RTO", "target": "RTO"}, {"source": "COD", "target": "COD"} ] }',
)  # Business context for better transcription of domain-specific terms
SONIOX_ENABLE_NON_FINAL_TOKENS = (
    os.environ.get("SONIOX_ENABLE_NON_FINAL_TOKENS", "false").lower() == "true"
)  # Enable interim/non-final tokens for real-time feedback
SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS = int(
    os.environ.get("SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS", "0")
)  # Maximum duration for non-final tokens (0 = no limit)
SONIOX_VAD_FORCE_TURN_ENDPOINT = (
    os.environ.get("SONIOX_VAD_FORCE_TURN_ENDPOINT", "false").lower() == "true"
)  # CRITICAL: false = Use Soniox intelligent endpoint detection
# true = Use external VAD (Silero)

# Smart Turn Configuration - These will be loaded lazily to avoid circular imports
# Required API key for FAL_SMART_TURN
FAL_SMART_TURN_API_KEY = os.getenv("FAL_SMART_TURN_API_KEY")

ENABLE_SMART_TURN = os.getenv("ENABLE_SMART_TURN", "false").lower() == "true"

# Automatic MCP Tool Server
ENABLE_BREEZE_MCP = os.environ.get("ENABLE_BREEZE_MCP", "false").lower() == "true"
ENABLE_BREEZE_MCP_FOR_BRET = (
    os.environ.get("ENABLE_BREEZE_MCP_FOR_BRET", "false").lower() == "true"
)

BREEZE_MCP_ENDPOINT_PATH = os.environ.get("BREEZE_MCP_ENDPOINT_PATH", "/ai/mcp/v2")

MCP_CLIENT_TIMEOUT = int(os.environ.get("MCP_CLIENT_TIMEOUT", 30))  # seconds
shops_for_mcp = os.environ.get("SHOPS_FOR_BREEZE_MCP", "")
SHOPS_FOR_BREEZE_MCP = [
    shop.strip() for shop in shops_for_mcp.split(",") if shop.strip()
]

# Shops for performance directives
shops_for_performance_directives_str = os.environ.get(
    "SHOPS_FOR_PERFORMANCE_DIRECTIVES", ""
)
SHOPS_FOR_PERFORMANCE_DIRECTIVES = [
    shop.strip()
    for shop in shops_for_performance_directives_str.split(",")
    if shop.strip()
]

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

ENABLE_WRITE_ACTIONS_FOR_MERCHANTS = (
    os.environ.get("ENABLE_WRITE_ACTIONS_FOR_MERCHANTS", "false").lower() == "true"
)

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

# KMS Configuration
SKIP_KMS_DECRYPT = os.getenv("SKIP_KMS_DECRYPT", "false").lower() == "true"
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")

# JWT Authentication Configuration
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "")
JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(
    os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "60")
)
LIGHTHOUSE_JWT_SECRET = os.getenv("LIGHTHOUSE_JWT_SECRET", "")
ENABLE_LIGHTHOUSE_AUTH = os.getenv("ENABLE_LIGHTHOUSE_AUTH", "false").lower() == "true"

BREEZE_BUDDY_VAD_CONFIDENCE = float(
    os.getenv("BREEZE_BUDDY_VAD_CONFIDENCE", "0.7")
)  # Require stronger confidence
BREEZE_BUDDY_VAD_START_SECS = float(
    os.getenv("BREEZE_BUDDY_VAD_START_SECS", "0.2")
)  # Pick up quicker
BREEZE_BUDDY_VAD_STOP_SECS = float(
    os.getenv("BREEZE_BUDDY_VAD_STOP_SECS", "0.8")
)  # Allow small pauses
BREEZE_BUDDY_VAD_MIN_VOLUME = float(
    os.getenv("BREEZE_BUDDY_VAD_MIN_VOLUME", "0.6")
)  # More tolerant for soft voice
BREEZE_BUDDY_STT_SERVICE = os.getenv(
    "BREEZE_BUDDY_STT_SERVICE", "soniox"
).lower()  # "google" or "openai"

# Session inactivity timeout
AUTOMATIC_SESSION_INACTIVITY_TIMEOUT = float(
    os.environ.get("AUTOMATIC_SESSION_INACTIVITY_TIMEOUT", 900.0)
)
MAX_DAILY_SESSION_LIMIT = int(os.environ.get("MAX_DAILY_SESSION_LIMIT", 1800))

# Pool Configuration
VOICE_AGENT_POOL_SIZE = int(os.environ.get("VOICE_AGENT_POOL_SIZE", 1))
VOICE_AGENT_MAX_POOL_SIZE = int(os.environ.get("VOICE_AGENT_MAX_POOL_SIZE", 3))
DAILY_ROOM_POOL_SIZE = int(os.environ.get("DAILY_ROOM_POOL_SIZE", 1))
DAILY_ROOM_MAX_POOL_SIZE = int(os.environ.get("DAILY_ROOM_MAX_POOL_SIZE", 5))

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
MAX_CHARTS_PER_TURN = int(os.environ.get("MAX_CHARTS_PER_TURN", "1"))

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

# Announcement Banner Configuration
DEFAULT_ANNOUNCEMENT_BANNER_TEXT_COLOR = os.environ.get(
    "DEFAULT_ANNOUNCEMENT_BANNER_TEXT_COLOR", "white"
)
DEFAULT_ANNOUNCEMENT_BANNER_BACKGROUND_COLOR = os.environ.get(
    "DEFAULT_ANNOUNCEMENT_BANNER_BACKGROUND_COLOR", "#714acd"
)

EXOTEL_ACCOUNT_SID = os.getenv("EXOTEL_ACCOUNT_SID", "")
EXOTEL_API_KEY = os.getenv("EXOTEL_API_KEY", "")
EXOTEL_API_TOKEN = os.getenv("EXOTEL_API_TOKEN", "")
AWS_VAYU_URL = os.environ.get("AWS_VAYU_URL")
AWS_VAYU_READ_API_KEY = os.environ.get("AWS_VAYU_READ_API_KEY")
AWS_VAYU_WRITE_API_KEY = os.environ.get("AWS_VAYU_WRITE_API_KEY")
EXOTEL_SUBDOMAIN = os.getenv("EXOTEL_SUBDOMAIN", "api.exotel.com")
EXOTEL_APPLET_APP_ID = os.getenv("EXOTEL_APPLET_APP_ID", "1044183")

# Proxy Configuration
AWS_PROXY_HOST = os.environ.get("AWS_PROXY_HOST")
AWS_PROXY_PORT = os.environ.get("AWS_PROXY_PORT")
CLOUD_ENVIRONMENT = os.environ.get("CLOUD_ENVIRONMENT", "GCP")  # AWS, GCP, AZURE, etc.

# LangFuse Configuration (for OpenTelemetry tracing only)
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "")
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_BASEURL = os.environ.get("LANGFUSE_BASEURL", "https://us.cloud.langfuse.com")

BREEZE_BUDDY_SONIOX_MODEL = os.environ.get(
    "BREEZE_BUDDY_SONIOX_MODEL", "stt-rt-preview"
)
BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS = os.environ.get(
    "BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS", "en,hi"
)
BREEZE_BUDDY_SONIOX_CONTEXT = os.environ.get(
    "BREEZE_BUDDY_SONIOX_CONTEXT",
    '{ "general": [ {"key": "organisation", "value": "Juspay"}, {"key": "company", "value": "Breeze"}, {"key": "product", "value": "Breeze Buddy"}, {"key": "domain", "value": "E-commerce Customer Service"}, {"key": "service_type", "value": "Order Confirmation and Address Verification"}, {"key": "conversation_type", "value": "Outbound automated voice call"}, {"key": "purpose", "value": "Cash on Delivery order confirmation"}, {"key": "user", "value": "Customer"}, {"key": "languages", "value": "Hindi, English, Tamil, Telugu, Kannada, Malayalam, Bengali, Marathi, Gujarati, and other Indian languages"}, {"key": "region", "value": "India"} ], "text": "Breeze Buddy is an automated voice agent that contacts customers across India who have placed Cash on Delivery orders. Customers may respond in any Indian language including Hindi, English, Tamil, Telugu, Kannada, Malayalam, Bengali, Marathi, Gujarati, or other regional languages. The agent confirms order details including product information, delivery address, contact number, and expected delivery date. The conversation involves verifying the customer\'s identity, confirming their order items and quantities, validating the complete delivery address including landmark details, and ensuring the customer will be available to receive the order. The agent handles common queries about order modifications, cancellations, and payment methods. Customers may use code-mixed language or switch between languages during the conversation.", "terms": [ "Juspay", "Breeze Buddy", "Shopify", "COD", "Cash on Delivery", "order confirmation", "delivery address", "pincode", "landmark", "order ID", "SKU", "order cancellation", "reschedule delivery", "prepaid", "payment gateway", "order tracking", "estimated delivery", "shipping address", "billing address", "State", "Yes", "Yeah", "Good", "Time", "Yep", "Later", "Available", "Busy", "Confirm", "Repeat", "What", "Order", "Hello", "Okay", "Sir", "Madam", "Namaste", "Address", "Price", "Delivery", "Rupees", "District", "Correct", "Fine", "Right", "Details", "Continue", "Item", "Total", "Cancel", "कौनसा", "ठीक है", "हाँ", "धन्यवाद", "ऑर्डर", "पता", "समय", "फोन", "संख्या", "बदलना", "सही है", "हेलो", "बोलिए", "जी", "मैडम", "नमस्ते", "कन्फर्म", "डिलीवरी", "पिनकोड", "रुपये", "कीमत", "राशि", "बराबर", "करेक्ट", "ओके" ], "translation_terms": [ {"source": "Juspay", "target": "Juspay"}, {"source": "Breeze", "target": "Breeze"}, {"source": "Breeze Buddy", "target": "Breeze Buddy"}, {"source": "Shopify", "target": "Shopify"}, {"source": "COD", "target": "COD"}, {"source": "Cash on Delivery", "target": "Cash on Delivery"}, {"source": "order ID", "target": "order ID"}, {"source": "Rhea", "target": "Rhea"}, {"source": "रिया", "target": "Rhea"} ] }',
)
BREEZE_BUDDY_SONIOX_ENABLE_NON_FINAL_TOKENS = (
    os.environ.get("BREEZE_BUDDY_SONIOX_ENABLE_NON_FINAL_TOKENS", "false").lower()
    == "true"
)
BREEZE_BUDDY_SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS = int(
    os.environ.get("BREEZE_BUDDY_SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS", "0")
)
BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT = (
    os.environ.get("BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT", "false").lower()
    == "true"
)

ENABLE_BREEZE_BUDDY_USER_INTERRUPTION = (
    os.environ.get("ENABLE_BREEZE_BUDDY_USER_INTERRUPTION", "false").lower() == "true"
)

BREEZE_BUDDY_PRE_ACTION_SPEAK_MESSAGE = os.environ.get(
    "BREEZE_BUDDY_PRE_ACTION_SPEAK_MESSAGE",
    "Okay",
)

# Dashboard Authentication
BREEZE_BUDDY_DASHBOARD_USERNAME = os.getenv("BREEZE_BUDDY_DASHBOARD_USERNAME", "")
BREEZE_BUDDY_DASHBOARD_PASSWORD = os.getenv("BREEZE_BUDDY_DASHBOARD_PASSWORD", "")
BREEZE_BUDDY_SESSION_SECRET_KEY = os.getenv("BREEZE_BUDDY_SESSION_SECRET_KEY", "")

ENABLE_BREEZE_BUDDY_TRACING = (
    os.getenv("ENABLE_BREEZE_BUDDY_TRACING", "false").lower() == "true"
)
BUDDY_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT = os.getenv(
    "BUDDY_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", ""
)
BUDDY_OTEL_EXPORTER_OTLP_TRACES_HEADERS = os.getenv(
    "BUDDY_OTEL_EXPORTER_OTLP_TRACES_HEADERS", ""
)
UPLOAD_BREEZE_BUDDY_CALL_RECORDINGS_TO_CLOUD = (
    os.getenv("UPLOAD_BREEZE_BUDDY_CALL_RECORDINGS_TO_CLOUD", "false").lower() == "true"
)

# Graceful Shutdown Configuration
# NOTE: BOT_MAX_DRAIN_SECONDS should be less than Kubernetes terminationGracePeriodSeconds
# to allow time for cleanup. Recommended: terminationGracePeriodSeconds - 20 seconds
# For a 45s termination grace period, use 25s drain + ~5s cleanup = 30s total
ENABLE_SIGTERM_HANDLER = (
    os.environ.get("ENABLE_SIGTERM_HANDLER", "false").lower() == "true"
)
BOT_MAX_DRAIN_SECONDS = int(os.environ.get("BOT_MAX_DRAIN_SECONDS", "25"))

# Redis Configuration
REDIS_HOST = os.getenv("REDIS_HOST", "")
REDIS_PORT = os.getenv("REDIS_PORT", "")
REDIS_CLUSTER_NODES = os.getenv("REDIS_CLUSTER_NODES", "")
REDIS_TTL = int(os.getenv("REDIS_TTL", "3600"))  # Default TTL in seconds (1 hour)

# DevCycle Configuration
DEVCYCLE_WEBHOOK_SECRET = os.getenv("DEVCYCLE_WEBHOOK_SECRET", "")
DEVCYCLE_SERVER_KEY = os.getenv("DEVCYCLE_SERVER_KEY", "")

# Langfuse Score Monitoring Configuration
LANGFUSE_EVALUATORS = [
    name.strip()
    for name in os.environ.get("LANGFUSE_EVALUATORS", "").split(",")
    if name.strip()
]

# Slack Webhook Configuration
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SLACK_TAG_USERS = os.environ.get("SLACK_TAG_USERS", "narsimha.reddy")

BACKGROUND_TASKS_LOOP_INTERVAL_SECONDS = int(
    os.environ.get("BACKGROUND_TASKS_LOOP_INTERVAL_SECONDS", "60")
)  # How often the scheduler checks tasks (in seconds)

# Langfuse Score Monitoring Configuration
ENABLE_BB_LANGFUSE_MONITORING_LOOP = (
    os.environ.get("ENABLE_BB_LANGFUSE_MONITORING_LOOP", "false").lower() == "true"
)
SCORE_CHECK_INTERVAL_SECONDS = int(
    os.environ.get("SCORE_CHECK_INTERVAL_SECONDS", "600")
)  # 10 minutes
