import math
import os

# --- Configuration ---


# Environment
ENVIRONMENT = os.environ.get("ENVIRONMENT", "production")
PROD_LOG_LEVEL = os.environ.get("PROD_LOG_LEVEL", "INFO")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "")

# DragonTTS URL and health timeout live in dynamic.py for runtime tuning.
# The monitor enable switch is intentionally static: env -> default only, read
# once at process startup, and never resolved through Redis.
ENABLE_DRAGONTTS_KILL_SWITCH = (
    os.environ.get("ENABLE_DRAGONTTS_KILL_SWITCH", "true").lower() == "true"
)

# Uvicorn
PORT = int(os.environ.get("PORT", 8000))
HOST = os.environ.get("HOST", "0.0.0.0")
UVICORN_RELOAD = os.environ.get("UVICORN_RELOAD", "true").lower() == "true"
UVICORN_LOG_LEVEL = os.environ.get("UVICORN_LOG_LEVEL", "info")

# Gemini Proxy Configuration
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Pipecat Agent Configuration
DAILY_API_KEY = os.environ.get("DAILY_API_KEY", "")
DAILY_API_URL = os.environ.get("DAILY_API_URL", "https://api.daily.co/v1")
# Breeze Buddy Daily API Configuration - falls back to DAILY_API_KEY and DAILY_API_URL if not set
BREEZE_BUDDY_DAILY_API_KEY = (
    os.environ.get("BREEZE_BUDDY_DAILY_API_KEY") or DAILY_API_KEY
)
BREEZE_BUDDY_DAILY_API_URL = (
    os.environ.get("BREEZE_BUDDY_DAILY_API_URL") or DAILY_API_URL
)
AZURE_OPENAI_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")

# GCS Configuration
GCS_CREDENTIALS_JSON = os.environ.get("GCS_CREDENTIALS_JSON", "")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "atoms-sdk")

# TTS Voice Catalog — preview storage (GCS only). Separate from GCS_BUCKET
# above (which is the recordings bucket) so previews can live in their own.
#
# Deliberately NOT validated at import: previews are one optional feature, and
# raising here would stop every deployment that never uses the voice catalog
# from booting. When the bucket is unset the catalog simply serves voices
# without previews (see preview_storage.load_manifest, which fails soft), and
# any attempt to *write* a preview raises with a message naming this variable.
TTS_PREVIEW_GCS_BUCKET = os.environ.get("TTS_PREVIEW_GCS_BUCKET", "")
# Optional: front the bucket with a CDN/custom domain. Defaults to the bucket's
# own public URL.
TTS_PREVIEW_PUBLIC_BASE_URL = os.environ.get("TTS_PREVIEW_PUBLIC_BASE_URL", "")

ENABLE_AIC_FILTER = os.environ.get("ENABLE_AIC_FILTER", "false").lower() == "true"
AIC_LICENSE_KEY = os.environ.get("AIC_LICENSE_KEY", "")
# Breeze Buddy AIC License Key
BREEZE_BUDDY_AIC_LICENSE_KEY = os.environ.get("BREEZE_BUDDY_AIC_LICENSE_KEY", "")

# AIC Filter Parameters (simplified for tuning)
AIC_ENHANCEMENT_LEVEL = float(os.environ.get("AIC_ENHANCEMENT_LEVEL", "1.0"))
AIC_VOICE_GAIN = float(os.environ.get("AIC_VOICE_GAIN", "1.2"))
AIC_NOISE_GATE_ENABLE = (
    os.environ.get("AIC_NOISE_GATE_ENABLE", "true").lower() == "true"
)

# AIC Model Path Configuration
AIC_MODEL_PATH = os.environ.get(
    "AIC_MODEL_PATH", "/app/models/voice/aic/quail_l_8khz.aicmodel"
)
AIC_MODEL_PATH_16KHZ = os.environ.get(
    "AIC_MODEL_PATH_16KHZ",
    "/app/models/voice/aic/quail_l_16khz.aicmodel",
)
AIC_VOICE_FOCUS_MODEL_PATH = os.environ.get(
    "AIC_VOICE_FOCUS_MODEL_PATH",
    "/app/models/voice/aic/quail_vf_2_1_l_16khz.aicmodel",
)

# TTS Configuration
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_INDIAN_RESIDENCY_API_KEY = os.environ.get(
    "ELEVENLABS_INDIAN_RESIDENCY_API_KEY"
)
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
ELEVENLABS_INDIAN_RESIDENCY_WEBSOCKET_URL = os.environ.get(
    "ELEVENLABS_INDIAN_RESIDENCY_WEBSOCKET_URL", "wss://api.in.residency.elevenlabs.io"
)
GOOGLE_BRET_VOICE = os.environ.get("GOOGLE_BRET_VOICE", "en-IN-Chirp3-HD-Sadaltager")
GOOGLE_MIA_VOICE = os.environ.get("GOOGLE_MIA_VOICE", "en-IN-Chirp3-HD-Despina")

# Cartesia TTS Configuration
CARTESIA_API_KEY = os.environ.get("CARTESIA_API_KEY")

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


# Tracing
ENABLE_TRACING = os.environ.get("ENABLE_TRACING", "false").lower() == "true"
OPEN_OBSERVE_BASE_URL = os.environ.get(
    "OPEN_OBSERVE_BASE_URL", "https://periscope.breeze.in"
)

# Text sanitization
SANITIZE_TEXT_FOR_TTS = (
    os.environ.get("SANITIZE_TEXT_FOR_TTS", "false").lower() == "true"
)


GEMINI_TRANSLATION_MODEL = os.environ.get(
    "GEMINI_TRANSLATION_MODEL", "gemini-2.5-flash"
)
TRANSLATION_TIMEOUT_SECONDS = int(os.environ.get("TRANSLATION_TIMEOUT_SECONDS", "30"))

# --- STT Configuration ---
STT_PROVIDER = os.environ.get(
    "STT_PROVIDER", "google"
).lower()  # "google", "assemblyai", "openai", "deepgram", "soniox", "elevenlabs", or "sarvam"
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
OPENAI_STT_API_KEY = os.getenv("OPENAI_STT_API_KEY")
OPENAI_STT_MODEL = os.environ.get(
    "OPENAI_STT_MODEL", "gpt-4o-transcribe"
)  # or "whisper-1"
ENFORCED_OPENAI_STT_MODEL = os.environ.get("ENFORCED_OPENAI_STT_MODEL", "whisper-1")
ENABLE_OPENAI_FOR_MIA = (
    os.environ.get("ENABLE_OPENAI_FOR_MIA", "false").lower() == "true"
)

# --- Deepgram STT ---
# Only API key lives here. All tuning params are in DeepgramSTTConfig (template)
# with sensible defaults — no env vars needed.
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

# --- Sarvam STT & TTS Configuration ---
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY")

# --- Soniox STT Configuration ---
# Soniox is optimized to solve the 0.5-second speech pause issue experienced with Deepgram
SONIOX_API_KEY = os.getenv(
    "SONIOX_API_KEY"
)  # Required API key for Soniox authentication
SONIOX_MODEL = os.environ.get(
    "SONIOX_MODEL", "stt-rt-v4"
)  # Soniox model optimized for real-time conversation
SONIOX_LANGUAGE_HINTS = os.environ.get(
    "SONIOX_LANGUAGE_HINTS", "en"
)  # Language hints for transcription (comma-separated: en,hi,es)
SONIOX_CONTEXT = os.environ.get(
    "SONIOX_CONTEXT",
    '{ "general": [ {"key": "organisation", "value": "Juspay"}, {"key": "company", "value": "Breeze"}, {"key": "product", "value": "Breeze Automatic"}, {"key": "related_product", "value": "Breeze Checkout"}, {"key": "domain", "value": "D2C ecommerce analytics and business intelligence"}, {"key": "service_type", "value": "AI chatbot for merchant data analysis"}, {"key": "data_sources", "value": "Shopify, payment gateways, Meta Ads, Google Ads, Google Analytics"}, {"key": "user", "value": "D2C merchant, brand owner, or marketing manager"}, {"key": "conversation_type", "value": "performance analysis, metrics review, optimization insights"} ], "text": "User Persona: D2C ecommerce merchants and marketing managers running Shopify stores who need quick, actionable insights from their business data. They are results-focused, time-constrained, and prefer asking natural questions over building complex reports. They monitor key metrics like revenue, conversion rates, CAC, and ROAS daily, making rapid decisions about budget allocation and optimization strategies.Breeze Automatic is an AI-powered analytics conversational chatbot designed specifically for direct-to-consumer ecommerce merchants. It collates and analyzes data from multiple sources including Shopify stores, payment gateways, Meta advertising platforms, Google Ads campaigns, and Google Analytics to provide comprehensive business insights. Merchants use Breeze Automatic to understand their checkout performance, advertising return on investment, conversion funnel optimization, and overall business health. Common conversational patterns include asking about daily or weekly revenue performance, comparing current metrics to previous time periods like month-over-month or year-over-year, investigating drops in conversion rates or spikes in customer acquisition costs, analyzing which advertising channels are performing best, understanding customer behavior and segmentation, tracking checkout abandonment rates, evaluating the effectiveness of marketing campaigns across Meta and Google platforms, and identifying opportunities to improve profitability. Merchants often inquire about their top-performing products, customer lifetime value trends, retention metrics, and how their ad spend efficiency compares across different channels. The chatbot helps answer questions like how todays sales compare to yesterday, which ad campaigns are driving the highest return on ad spend, why checkout conversion might be declining, what the blended customer acquisition cost is across all channels, and how to allocate budget between Meta Ads and Google Ads for maximum return. Users discuss payment success rates, failed transactions, refund patterns, and fraud indicators. They ask about traffic sources, landing page performance, and which marketing touchpoints contribute most to conversions. Breeze Automatic enables merchants to make data-driven decisions by translating complex analytics into actionable insights through natural conversation.", "terms": [ "Juspay", "Breeze Automatic", "Breeze Checkout", "Shopify", "Razorpay", "Cashfree", "PayU", "Easebuzz", "Meta Ads", "Google Ads", "Google Analytics", "D2C", "PSR", "GMV", "UPI", "ROAS", "AOV", "RTO", "COD", "CAC", "LTV", "CPC", "CPM", "CTR", "NDR", "AWB", "SKU", "Sales", "Cart", "Abandonment", "Split", "Yesterday", "Dispatches", "Orders", "Fulfillment", "Processing", "Shipped", "Delivered", "In-transit", "Return", "Exchange", "Refund", "Settlement", "Payout", "Transaction", "Failed payment", "Payment pending", "Chargeback", "Disputed transaction", "Payment success rate", "Non-delivery report", "Undelivered", "Pin code", "Serviceable", "New customer", "Returning customer", "Repeat purchase", "Customer cohort", "Customer segment", "First-time buyer", "Repeat buyer", "Campaign", "Ad set", "Creative", "Cost per click", "Cost per mille", "Click-through rate", "Impression", "Reach", "Engagement", "Conversion", "Pixel", "Attribution", "Conversion rate", "Bounce rate", "Sessions", "Pageviews", "Unique visitors", "Add-to-cart rate", "Checkout drop-off", "blended CAC", "checkout abandonment", "conversion funnel", "ad spend", "Google Ads Spend", "customer acquisition cost", "return on ad spend", "month-over-month", "year-over-year", "payment gateway", "customer lifetime value", "marketing touchpoints", "landing page performance", "traffic sources", "checkout conversion", "refund patterns", "fraud indicators", "retention metrics", "budget allocation", "ecommerce", "direct-to-consumer", "Out of stock", "Inventory turnover" ], "translation_terms": [ {"source": "Juspay", "target": "Juspay"}, {"source": "Breeze", "target": "Breeze"}, {"source": "Shopify", "target": "Shopify"}, {"source": "Razorpay", "target": "Razorpay"}, {"source": "Cashfree", "target": "Cashfree"}, {"source": "PayU", "target": "PayU"}, {"source": "Easebuzz", "target": "Easebuzz"}, {"source": "Meta Ads", "target": "Meta Ads"}, {"source": "Google Ads", "target": "Google Ads"}, {"source": "ROAS", "target": "ROAS"}, {"source": "CAC", "target": "CAC"}, {"source": "D2C", "target": "D2C"}, {"source": "PSR", "target": "PSR"}, {"source": "GMV", "target": "GMV"}, {"source": "UPI", "target": "UPI"}, {"source": "AOV", "target": "AOV"}, {"source": "RTO", "target": "RTO"}, {"source": "COD", "target": "COD"} ] }',
)  # Business context for better transcription of domain-specific terms
SONIOX_VAD_FORCE_TURN_ENDPOINT = (
    os.environ.get("SONIOX_VAD_FORCE_TURN_ENDPOINT", "false").lower() == "true"
)  # CRITICAL: false = Use Soniox intelligent endpoint detection
# true = Use external VAD (Silero)
SONIOX_MAX_ENDPOINT_DELAY_MS = int(
    os.environ.get("SONIOX_MAX_ENDPOINT_DELAY_MS", "500")
)  # Max delay (ms) for Soniox native endpoint detection (500-3000, default 500)


LIGHTHOUSE_APP_URL = os.environ.get("LIGHTHOUSE_APP_URL", "http://localhost:5173")


LOOM_APP_URL = os.environ.get("LOOM_APP_URL", "https://breezebuddy.ai")


AZURE_BREEZE_BUDDY_OPENAI_MODEL = os.environ.get(
    "AZURE_BREEZE_BUDDY_OPENAI_MODEL", "gpt-4o-automatic"
)

# Chat (text-mode) idle-cleanup sweep cadence. Bound once at startup by
# BackgroundTaskScheduler.register_task — a change requires a pod restart.
# The threshold itself (CHAT_SESSION_END_TIMEOUT_SECONDS, in dynamic.py)
# stays live-tunable. Default 5 minutes.
CHAT_SESSION_END_TIMEOUT_LOOP_INTERVAL_SECONDS = int(
    os.environ.get("CHAT_SESSION_END_TIMEOUT_LOOP_INTERVAL_SECONDS", 300)
)


# ---------------------------------------------------------------------------
# Public chat demo (CHAT_MODE.md §13) — structural config
# ---------------------------------------------------------------------------
#
# DEMO_TEMPLATES is a comma-separated ``slug:template_id`` list, parsed
# once at import. Visitors pass the slug in the JSON body of
# ``POST /chat/demo/session``; the demo router resolves it to the
# template_id below. Adding a new demo = env var change + pod restart;
# no DB migration. Anything not listed here is invisible to the demo
# router, so an accidentally chat-enabled production template can't
# leak through.
#
# This stays static (rather than living in dynamic.py) because the value
# is structured (parsed dict) and adding/removing demos is operationally
# infrequent. The *tuning* knobs (caps, rate limits, token TTL) live in
# dynamic.py and can be dialed without a restart.
def _parse_demo_templates(raw: str) -> dict[str, str]:
    """Parse 'slug:template_id,slug:template_id' pairs; malformed entries are skipped."""
    out: dict[str, str] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            continue
        slug, template_id = entry.split(":", 1)
        slug = slug.strip()
        template_id = template_id.strip()
        if slug and template_id:
            out[slug] = template_id
    return out


DEMO_TEMPLATES: dict[str, str] = _parse_demo_templates(
    os.environ.get("DEMO_TEMPLATES", "")
)


# Twilio settings
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_TEMPLATE_WEBSOCKET_URL = os.getenv("TWILIO_TEMPLATE_WEBSOCKET_URL", "")
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

# Worker threads available to asyncio.to_thread() for offloaded blocking work
# (sync telephony SDKs: Plivo recording, provider.make_call, Twilio conference).
# Python's default is min(32, cpu_count + 4) — only 5 threads on a 1-core pod,
# which re-creates the queueing problem one layer down at ~30 calls/sec.
# These threads are I/O-bound, so oversubscribing relative to cores is correct.
# Threads are created lazily (~37 KB RSS each, +1.7 MB if all 48 go live).
#
# Shared pool caveat: this is the loop's DEFAULT executor, used by every
# asyncio.to_thread()/run_in_executor(None, ...) caller. Note that
# template/global_function.py wraps sync handlers in asyncio.wait_for --
# which cancels the await but does NOT stop the worker thread, so a timed-out
# global function keeps its slot until the handler actually returns. That
# hazard predates this setting; a 48-slot pool tolerates it far better than
# the 5-slot Python default it replaces.
BLOCKING_THREAD_POOL_SIZE = int(os.getenv("BLOCKING_THREAD_POOL_SIZE", "48"))
if BLOCKING_THREAD_POOL_SIZE < 1:
    # ThreadPoolExecutor(max_workers=0) raises, which would kill startup with a
    # stack trace pointing at main.py rather than at the misconfigured env var.
    raise ValueError(
        f"BLOCKING_THREAD_POOL_SIZE must be >= 1, got {BLOCKING_THREAD_POOL_SIZE}"
    )

# Daily voice bot subprocess (per-call child processes; see
# breeze_buddy/services/daily). Kept next to the API pool settings above so
# the two are tuned together.
# Per-child asyncpg pool: bot_runner calls
# init_db_pool(min_size=BB_VOICE_BOT_DB_POOL_SIZE, max_size=POOL+OVERFLOW)
# explicitly — a per-call child must stay far below the API pod's defaults.
BB_VOICE_BOT_DB_POOL_SIZE = int(os.environ.get("BB_VOICE_BOT_DB_POOL_SIZE", "1"))
BB_VOICE_BOT_DB_MAX_OVERFLOW = int(os.environ.get("BB_VOICE_BOT_DB_MAX_OVERFLOW", "2"))
# Per-pod cap on live Daily bots (each is one OS process + 1-3 Postgres
# connections + a Redis connection + ~300MB RSS). start_daily_session rejects
# new sessions above this instead of letting a spike exhaust Postgres
# max_connections.
BB_MAX_CONCURRENT_DAILY_BOTS = int(os.environ.get("BB_MAX_CONCURRENT_DAILY_BOTS", "20"))
# Watchdog: kill a bot child still alive past this. Healthy calls end at the
# 1h Daily room expiry, so anything older is wedged (e.g. a stuck
# daily-python native thread) and would leak its process/connections forever.
BB_DAILY_BOT_MAX_LIFETIME_SECS = int(
    os.environ.get("BB_DAILY_BOT_MAX_LIFETIME_SECS", "4500")
)

# KMS Configuration
SKIP_KMS_DECRYPT = os.getenv("SKIP_KMS_DECRYPT", "false").lower() == "true"
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")

# Credential Encryption (AES-256-GCM)
# Base64-encoded 32-byte key. Generate with:
#   python -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
# When empty, credentials are stored as plain JSON (acceptable for dev/local).
CREDENTIAL_ENCRYPTION_KEY = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "")

# JWT Authentication Configuration
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "")
JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(
    os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "60")
)
LIGHTHOUSE_JWT_SECRET = os.getenv("LIGHTHOUSE_JWT_SECRET", "")
ENABLE_LIGHTHOUSE_AUTH = os.getenv("ENABLE_LIGHTHOUSE_AUTH", "false").lower() == "true"

# Google OAuth (for SSO login and self-service merchant signup)
# Obtain from: https://console.cloud.google.com/apis/credentials
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")

# Umbrella reseller stamped on every self-registered merchant (signup
# routes). Kept separate from "breeze" (manual onboarding) so self-serve
# accounts are distinguishable in every reseller-scoped view; override for
# staging or an instant rollback without a deploy.
SELF_SIGNUP_RESELLER_ID = os.getenv("BB_SELF_SIGNUP_RESELLER_ID", "breeze-self-serve")

BREEZE_BUDDY_STT_SERVICE = os.getenv(
    "BREEZE_BUDDY_STT_SERVICE", "soniox"
).lower()  # "soniox", "sarvam", "openai", "deepgram", or "google"


# Breeze Portal URLs
AWS_BREEZE_PORTAL_URL = os.environ.get(
    "AWS_BREEZE_PORTAL_URL", "https://portal.breeze.in"
)
GCP_BREEZE_PORTAL_URL = os.environ.get(
    "GCP_BREEZE_PORTAL_URL", "https://portal.breezesdk.store"
)

# -----------------------------------------------------------------------------
# Event-Driven Dispatch (Breeze Buddy backlog dispatcher)
# See docs/BACKLOG_DISPATCHER_REDESIGN.md
# -----------------------------------------------------------------------------

# Pod-role split. Same code runs as two k8s workloads today.
#   main_server  -> serves HTTP, runs dispatcher + reconcilers
#   agent_pool   -> runs voice agent subprocesses + Daily room pool only
POD_ROLE = os.environ.get("POD_ROLE", "main_server").lower()


def _flag(env_var: str, default_main_server: bool, default_agent_pool: bool) -> bool:
    """
    Component flags default from POD_ROLE; explicit env var overrides.
    """
    val = os.environ.get(env_var)
    if val is not None:
        return val.lower() == "true"
    if POD_ROLE == "agent_pool":
        return default_agent_pool
    return default_main_server


ENABLE_DISPATCHER = _flag("ENABLE_DISPATCHER", True, False)

# Promoter
BB_PROMOTER_TICK_MS = int(os.environ.get("BB_PROMOTER_TICK_MS", 200))
BB_PROMOTER_BATCH = int(os.environ.get("BB_PROMOTER_BATCH", 500))
BB_PROMOTER_LEADER_TTL_S = int(os.environ.get("BB_PROMOTER_LEADER_TTL_S", 5))
BB_PROMOTER_LEADER_RENEW_S = int(os.environ.get("BB_PROMOTER_LEADER_RENEW_S", 2))

# Workers
# Default count is environment-aware: dev pods only need a couple of BLPOP
# loops for smoke-testing the dispatcher path; production sizes for real
# outbound load. Explicit BB_WORKER_COUNT in the env wins over the default.
_BB_WORKER_COUNT_DEFAULT = 2 if ENVIRONMENT == "dev" else 20
BB_WORKER_COUNT = int(os.environ.get("BB_WORKER_COUNT", _BB_WORKER_COUNT_DEFAULT))
BB_WORKER_BLPOP_TIMEOUT_S = int(os.environ.get("BB_WORKER_BLPOP_TIMEOUT_S", 30))
BB_WORKER_HEARTBEAT_TTL_S = int(os.environ.get("BB_WORKER_HEARTBEAT_TTL_S", 60))
BB_WORKER_HEARTBEAT_REFRESH_S = int(os.environ.get("BB_WORKER_HEARTBEAT_REFRESH_S", 10))

# Channel semaphore
BB_CHANNEL_BLPOP_TIMEOUT_S = int(os.environ.get("BB_CHANNEL_BLPOP_TIMEOUT_S", 10))
BB_CHANNEL_WAIT_BACKOFF_MAX_S = int(os.environ.get("BB_CHANNEL_WAIT_BACKOFF_MAX_S", 3))

# Reconcilers
BB_RECONCILE_BACKLOG_INTERVAL_S = int(
    os.environ.get("BB_RECONCILE_BACKLOG_INTERVAL_S", 60)
)
BB_REAP_PROCESSING_INTERVAL_S = int(os.environ.get("BB_REAP_PROCESSING_INTERVAL_S", 30))
BB_RECONCILE_CHANNELS_INTERVAL_S = int(
    os.environ.get("BB_RECONCILE_CHANNELS_INTERVAL_S", 60)
)
BB_CLEAN_STALE_LOCKS_INTERVAL_S = int(
    os.environ.get("BB_CLEAN_STALE_LOCKS_INTERVAL_S", 300)
)
# Stuck-PROCESSING reconciler — closes calls whose end-webhook never arrived.
BB_RECONCILE_STUCK_PROCESSING_INTERVAL_S = int(
    os.environ.get("BB_RECONCILE_STUCK_PROCESSING_INTERVAL_S", 60)
)

# Health monitor — periodic Slack alerter (alert-only, no state mutation).
BB_HEALTH_MONITOR_INTERVAL_S = int(os.environ.get("BB_HEALTH_MONITOR_INTERVAL_S", 60))

# Ingest / retry jitter (±ms applied to every ZADD score). Smooths bursts.
BB_DISPATCH_QPS_JITTER_MS = int(os.environ.get("BB_DISPATCH_QPS_JITTER_MS", 200))

# NOTE: the following dispatcher dials moved to app/core/config/dynamic.py so
# ops can tune them at runtime via DevCycle without a redeploy:
#   - BB_SCHEDULE_DEPTH_ALERT_THRESHOLD
#   - BB_SCHEDULE_OVERDUE_ALERT_THRESHOLD
#   - BB_CHANNEL_DRIFT_ALERT_THRESHOLD
#   - BB_STALE_LOCK_THRESHOLD_MINUTES
#   - BB_RECONCILE_BACKLOG_LIMIT


def _positive_int(env_var: str, default: int) -> int:
    """Read a positive int, falling back to the default on garbage or <1.

    Lenient rather than fatal: a bad dial should not stop a pod from booting.
    But it must not be honoured either — a zero here would dead-letter every
    message on its first attempt, or expire a claim lease mid-send.
    """
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


def _positive_float(env_var: str, default: float) -> float:
    """Read a positive finite float, falling back to the default.

    Same leniency as _positive_int. Finite matters: 'inf' parses and compares
    > 0, and an infinite poll interval is a worker that sleeps forever after
    its first empty poll — draining nothing and raising nothing.
    """
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default


# -----------------------------------------------------------------------------
# CRM worker roles (design/worker-runtime.md, sealed 26-Aug-2026)
# One image, N pods: CRM_ROLE selects which drain loop (if any) this
# replica runs. Independent of POD_ROLE — a CRM worker pod never serves
# HTTP or the dispatcher. "api" (default) starts no loop.
# -----------------------------------------------------------------------------
CRM_ROLE = os.environ.get("CRM_ROLE", "api").lower()

CRM_WORKER_INTERVAL = _positive_float("CRM_WORKER_INTERVAL", 1.0)
CRM_WORKER_BATCH = _positive_int("CRM_WORKER_BATCH", 100)
CRM_WORKER_HEARTBEAT = _positive_float("CRM_WORKER_HEARTBEAT", 60.0)
# Event worker: every claim spends one attempt on the row (migration 062,
# the enrollment.attempts shape); a row whose consumer still raises at
# this count is quarantined instead of sitting at the head of the queue
# forever. Replay (clear processed_at/quarantine_reason) re-drives it.
CRM_EVENT_MAX_ATTEMPTS = _positive_int("CRM_EVENT_MAX_ATTEMPTS", 5)

# CRM outbound dispatcher (runs only when CRM_ROLE=dispatcher; pacing rides
# CRM_WORKER_INTERVAL, but the batch is its own dial below). send() reaches
# real providers behind a thin permission slice: dispatch._gate probes
# platform suppression before every send and fails CLOSED. The full
# may_contact() — consent, purpose, quiet hours — replaces the gate's body
# at B5; the seam is already load-bearing.

# The dispatcher's own batch — deliberately NOT CRM_WORKER_BATCH (100).
# Sends are serial and each may spend the full send timeout, so claiming a
# batch is a promise to finish batch × timeout of work inside the claim
# lease. At 100 that promise breaks (100 × 20s ≫ the lease) and another pod's
# sweep re-sends the unworked tail while this pod still holds it — a REAL
# duplicate message, which no outcome guard can undo.
CRM_DISPATCH_BATCH = _positive_int("CRM_DISPATCH_BATCH", 20)

# How long a worker may hold a row before it is assumed dead and requeued.
# Must exceed a whole batch of worst-case sends — CRM_DISPATCH_BATCH × 2 ×
# CRM_MESSAGE_SEND_TIMEOUT_SECONDS, the 2 because each message may burn one
# timeout in the gate probe and another in send() — since rows claimed at
# the head of a pass sit in 'sending' while the tail is worked. Defaults
# give 20 × 2 × 20s = 800s against 900s — the bound the test suite pins so
# no dial can drift past the others. The only cost of a longer lease is how
# long a genuinely dead worker's rows wait for rescue.
CRM_DISPATCH_STALE_MINUTES = _positive_int("CRM_DISPATCH_STALE_MINUTES", 15)

# Bounded so one undeliverable message cannot earn a provider rate-limit ban
# for every other merchant sharing that sender.
CRM_DISPATCH_MAX_ATTEMPTS = _positive_int("CRM_DISPATCH_MAX_ATTEMPTS", 3)

# Retry backoff: a provider answering "you are sending too fast" must be
# obeyed, so each attempt waits twice as long as the last.
CRM_DISPATCH_RETRY_BASE_SECONDS = _positive_int("CRM_DISPATCH_RETRY_BASE_SECONDS", 30)

# The ceiling on ONE provider call, applied by send() so that no adapter can
# forget it — and separately on the gate probe before it (dispatch._gate),
# which reads the same pool. Must stay well under CRM_DISPATCH_STALE_MINUTES:
# a send that outlives its claim gets the row reassigned to a second worker
# while the first is still sending, and the customer receives the message
# twice.
CRM_MESSAGE_SEND_TIMEOUT_SECONDS = _positive_int("CRM_MESSAGE_SEND_TIMEOUT_SECONDS", 20)

# Meta WhatsApp Cloud API. Only the endpoint lives here — the access token and
# the phone number id are per-merchant connector data, read from the vault at
# send time. Pointing the base URL at a local stub is how the dispatcher is
# exercised end to end without sending anything to Meta.
META_WHATSAPP_GRAPH_BASE_URL = os.environ.get(
    "META_WHATSAPP_GRAPH_BASE_URL", "https://graph.facebook.com"
)
META_WHATSAPP_GRAPH_VERSION = os.environ.get("META_WHATSAPP_GRAPH_VERSION", "v23.0")

# The app identity behind Embedded Signup and every non-send Graph call
# (code exchange, template registration). APP_SECRET is also what will verify
# inbound Meta webhooks — the same secret proves both directions.
# Empty is a legal boot: a deployment that never connects WhatsApp needs
# neither, and a Graph call without them fails loudly at the call, not at
# import (there is no get_required_env() in this repo to lean on).
META_APP_ID = os.environ.get("META_APP_ID", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")


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
# Exotel Webhook Authentication - required for inbound webhook security
EXOTEL_WEBHOOK_AUTH_TOKEN = os.getenv("EXOTEL_WEBHOOK_AUTH_TOKEN", "")
AWS_VAYU_URL = os.environ.get("AWS_VAYU_URL")
AWS_VAYU_READ_API_KEY = os.environ.get("AWS_VAYU_READ_API_KEY")
AWS_VAYU_WRITE_API_KEY = os.environ.get("AWS_VAYU_WRITE_API_KEY")
EXOTEL_SUBDOMAIN = os.getenv("EXOTEL_SUBDOMAIN", "api.exotel.com")
EXOTEL_TEMPLATE_APPLET_APP_ID = os.getenv("EXOTEL_TEMPLATE_APPLET_APP_ID", "")

# Plivo Configuration
PLIVO_AUTH_ID = os.getenv("PLIVO_AUTH_ID", "")
PLIVO_AUTH_TOKEN = os.getenv("PLIVO_AUTH_TOKEN", "")
PLIVO_RECORDING_TIME_LIMIT = int(
    os.getenv("PLIVO_RECORDING_TIME_LIMIT", "14400")
)  # Default: 4 hours (14400 seconds)
# PLIVO_INR_CONVERSION_RATE lives in dynamic.py (Redis-backed) -- it drifts
# with the market and updating it shouldn't need a pod restart.

# Proxy Configuration
AWS_PROXY_HOST = os.environ.get("AWS_PROXY_HOST")
AWS_PROXY_PORT = os.environ.get("AWS_PROXY_PORT")
CLOUD_ENVIRONMENT = os.environ.get("CLOUD_ENVIRONMENT", "GCP")  # AWS, GCP, AZURE, etc.

# --- Google Vertex AI (template generator) ---
# Credentials JSON and project ID stay in dynamic.py (Redis-backed).
# Region and model are structural — a pod restart on change is acceptable.
TEMPLATE_BUILDER_VERTEX_REGION = os.environ.get(
    "TEMPLATE_BUILDER_VERTEX_REGION", "us-east5"
)
TEMPLATE_BUILDER_VERTEX_CLAUDE_MODEL = os.environ.get(
    "TEMPLATE_BUILDER_VERTEX_CLAUDE_MODEL", "claude-sonnet-4@20250514"
)
TEMPLATE_BUILDER_MAX_TOKENS = int(
    os.environ.get("TEMPLATE_BUILDER_MAX_TOKENS", "16000")
)

# LangFuse Configuration (for OpenTelemetry tracing only)
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "")
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_BASEURL = os.environ.get("LANGFUSE_BASEURL", "https://us.cloud.langfuse.com")

BREEZE_BUDDY_SONIOX_MODEL = os.environ.get("BREEZE_BUDDY_SONIOX_MODEL", "stt-rt-v4")
BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS = os.environ.get(
    "BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS", "en,hi"
)
BREEZE_BUDDY_SONIOX_CONTEXT = os.environ.get(
    "BREEZE_BUDDY_SONIOX_CONTEXT",
    '{ "general": [ {"key": "organisation", "value": "Juspay"}, {"key": "company", "value": "Breeze"}, {"key": "product", "value": "Breeze Buddy"}, {"key": "domain", "value": "E-commerce Customer Service"}, {"key": "service_type", "value": "Order Confirmation and Address Verification"}, {"key": "conversation_type", "value": "Outbound automated voice call"}, {"key": "purpose", "value": "Cash on Delivery order confirmation"}, {"key": "user", "value": "Customer"}, {"key": "languages", "value": "Hindi, English, Tamil, Telugu, Kannada, Malayalam, Bengali, Marathi, Gujarati, and other Indian languages"}, {"key": "region", "value": "India"} ], "text": "Breeze Buddy is an automated voice agent that contacts customers across India who have placed Cash on Delivery orders. Customers may respond in any Indian language including Hindi, English, Tamil, Telugu, Kannada, Malayalam, Bengali, Marathi, Gujarati, or other regional languages. The agent confirms order details including product information, delivery address, contact number, and expected delivery date. The conversation involves verifying the customer\'s identity, confirming their order items and quantities, validating the complete delivery address including landmark details, and ensuring the customer will be available to receive the order. The agent handles common queries about order modifications, cancellations, and payment methods. Customers may use code-mixed language or switch between languages during the conversation.", "terms": [ "Juspay", "Breeze Buddy", "Shopify", "COD", "Cash on Delivery", "order confirmation", "delivery address", "pincode", "landmark", "order ID", "SKU", "order cancellation", "reschedule delivery", "prepaid", "payment gateway", "order tracking", "estimated delivery", "shipping address", "billing address", "State", "Yes", "Yeah", "Good", "Time", "Yep", "Later", "Available", "Busy", "Confirm", "Repeat", "What", "Order", "Hello", "Okay", "Sir", "Madam", "Namaste", "Address", "Price", "Delivery", "Rupees", "District", "Correct", "Fine", "Right", "Details", "Continue", "Item", "Total", "Cancel", "कौनसा", "ठीक है", "हाँ", "धन्यवाद", "ऑर्डर", "पता", "समय", "फोन", "संख्या", "बदलना", "सही है", "हेलो", "बोलिए", "जी", "मैडम", "नमस्ते", "कन्फर्म", "डिलीवरी", "पिनकोड", "रुपये", "कीमत", "राशि", "बराबर", "करेक्ट", "ओके" ], "translation_terms": [ {"source": "Juspay", "target": "Juspay"}, {"source": "Breeze", "target": "Breeze"}, {"source": "Breeze Buddy", "target": "Breeze Buddy"}, {"source": "Shopify", "target": "Shopify"}, {"source": "COD", "target": "COD"}, {"source": "Cash on Delivery", "target": "Cash on Delivery"}, {"source": "order ID", "target": "order ID"}, {"source": "Rhea", "target": "Rhea"}, {"source": "रिया", "target": "Rhea"} ] }',
)
BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT = (
    os.environ.get("BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT", "false").lower()
    == "true"
)
BREEZE_BUDDY_SONIOX_MAX_ENDPOINT_DELAY_MS = int(
    os.environ.get("BREEZE_BUDDY_SONIOX_MAX_ENDPOINT_DELAY_MS", "500")
)  # Max delay (ms) for Soniox native endpoint detection (500-3000, default 500)

ENABLE_BREEZE_BUDDY_USER_INTERRUPTION = (
    os.environ.get("ENABLE_BREEZE_BUDDY_USER_INTERRUPTION", "false").lower() == "true"
)

ENABLE_BREEZE_BUDDY_TRACING = (
    os.getenv("ENABLE_BREEZE_BUDDY_TRACING", "false").lower() == "true"
)
ENABLE_BREEZE_BUDDY_DAILY_EVENTS = (
    os.getenv("ENABLE_BREEZE_BUDDY_DAILY_EVENTS", "true").lower() == "true"
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
# Brute-force rate limiting for the unauthenticated credential endpoints
# (/login, /auth/s2s/token, /signup, /auth/accounts). Fixed-window caps per
# client IP and per username/email, layered on top of bcrypt + the timing
# equalization (PT-16) so online guessing is bounded. Set a cap to 0 to disable
# that dimension. Enforcement fails OPEN on a Redis outage (see
# app/api/routers/breeze_buddy/auth/rate_limit.py) so a Redis blip can't lock
# every operator out of login.
AUTH_RATE_LIMIT_PER_IP_PER_HOUR = int(
    os.environ.get("AUTH_RATE_LIMIT_PER_IP_PER_HOUR", "40")
)
AUTH_RATE_LIMIT_PER_USERNAME_PER_HOUR = int(
    os.environ.get("AUTH_RATE_LIMIT_PER_USERNAME_PER_HOUR", "15")
)

REDIS_HOST = os.getenv("REDIS_HOST", "")
REDIS_PORT = os.getenv("REDIS_PORT", "")
REDIS_CLUSTER_NODES = os.getenv("REDIS_CLUSTER_NODES", "")
REDIS_TTL = int(os.getenv("REDIS_TTL", "3600"))  # Default TTL in seconds (1 hour)
BLACKLIST_CACHE_TTL = int(os.getenv("BLACKLIST_CACHE_TTL", "300"))  # 5 minutes

# ─────────────────────────────────────────────────────────────────────────────
# Voice Agent Pod Isolation Configuration (1-pod-1-call architecture)
# ─────────────────────────────────────────────────────────────────────────────
# Enable pod isolation mode (when True, uses 1-pod-1-call architecture)
ENABLE_VOICE_AGENT_POD_ISOLATION = (
    os.environ.get("ENABLE_VOICE_AGENT_POD_ISOLATION", "false").lower() == "true"
)

# Pod identity (injected by Kubernetes StatefulSet)
# POD_NAME is the unique identifier like "voice-agent-0"
VOICE_AGENT_POD_NAME = os.environ.get("POD_NAME", "")
VOICE_AGENT_POD_IP = os.environ.get("POD_IP", "")


# =============================================================================
# Smart Router Configuration
# =============================================================================
# Smart Router is an external service that handles pod allocation and pool management
# When enabled, allocation logic moves from Python to Smart Router service

# Base URL for Smart Router API
SMART_ROUTER_BASE_URL = os.environ.get(
    "SMART_ROUTER_BASE_URL", "http://smart-router:8080"
)

# API Key for Smart Router authentication (if required)
SMART_ROUTER_API_KEY = os.environ.get("SMART_ROUTER_API_KEY", "")

# Timeout for Smart Router API calls (milliseconds)
SMART_ROUTER_TIMEOUT_MS = int(os.environ.get("SMART_ROUTER_TIMEOUT_MS", "3000"))

# Number of retry attempts for failed allocations
SMART_ROUTER_RETRY_ATTEMPTS = int(os.environ.get("SMART_ROUTER_RETRY_ATTEMPTS", "3"))

# Circuit Breaker Configuration
# Number of consecutive failures before opening circuit
SMART_ROUTER_CB_FAILURE_THRESHOLD = int(
    os.environ.get("SMART_ROUTER_CB_FAILURE_THRESHOLD", "5")
)
# Seconds to wait before trying to recover from open circuit
SMART_ROUTER_CB_RECOVERY_TIMEOUT = int(
    os.environ.get("SMART_ROUTER_CB_RECOVERY_TIMEOUT", "30")
)
# Number of successful calls in half-open state before closing circuit
SMART_ROUTER_CB_HALF_OPEN_MAX_CALLS = int(
    os.environ.get("SMART_ROUTER_CB_HALF_OPEN_MAX_CALLS", "3")
)

# When True (default), dynamic config keys are fetched from Redis first, then fall back to env/default.
# When False, Redis is skipped entirely for dynamic config — all keys resolve from env/default directly.
ENABLE_REDIS_DYNAMIC_CONFIG = (
    os.getenv("ENABLE_REDIS_DYNAMIC_CONFIG", "true").lower() == "true"
)

# DevCycle Configuration
DEVCYCLE_WEBHOOK_SECRET = os.getenv("DEVCYCLE_WEBHOOK_SECRET", "")
DEVCYCLE_SERVER_KEY = os.getenv("DEVCYCLE_SERVER_KEY", "")

# Slack Webhook Configuration
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SLACK_TAG_USERS = os.environ.get("SLACK_TAG_USERS", "narsimha.reddy")

BACKGROUND_TASKS_LOOP_INTERVAL_SECONDS = int(
    os.environ.get("BACKGROUND_TASKS_LOOP_INTERVAL_SECONDS", "60")
)  # How often the scheduler checks tasks (in seconds). Pre-existing shared
# cadence for ALL background tasks (chat cleanup, kb ingestion, reconcilers,
# the DragonTTS kill-switch probe, ...).

# Langfuse Score Monitoring Configuration
ENABLE_BB_LANGFUSE_MONITORING_LOOP = (
    os.environ.get("ENABLE_BB_LANGFUSE_MONITORING_LOOP", "false").lower() == "true"
)
SCORE_CHECK_INTERVAL_SECONDS = int(
    os.environ.get("SCORE_CHECK_INTERVAL_SECONDS", "600")
)  # 10 minutes

# CORS Configuration
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        "https://buddy.breezelabs.app,https://portal.breeze.in,https://portal.breezesdk.store",
    ).split(",")
    if origin.strip()
]

# Template `custom` global functions execute author-supplied python_code. An
# in-process interpreter sandbox is NOT a security boundary, so this feature is
# OFF by default and must be explicitly enabled per-deployment (and only when
# template authorship is trusted). When disabled, custom python_code functions
# are skipped at flow-build time and never compiled or executed.
ENABLE_CUSTOM_PYTHON_FUNCTIONS = (
    os.environ.get("ENABLE_CUSTOM_PYTHON_FUNCTIONS", "false").lower() == "true"
)

GLOBAL_FUNCTION_DESCRIPTION_SUFFIX = os.environ.get(
    "GLOBAL_FUNCTION_DESCRIPTION_SUFFIX",
    "After providing the answer, continue the conversation from where it was interrupted, reminding the user of the current step or asking the next relevant question.",
)

# HTTP Request Security Configuration (for hooks and global functions)
# Maximum response size in bytes to prevent downloading huge files
HTTP_REQUEST_MAX_RESPONSE_BYTES = int(
    os.environ.get("HTTP_REQUEST_MAX_RESPONSE_BYTES", str(100 * 1024))  # 100KB default
)

# Content types that are blocked to prevent downloading executables/scripts
# Comma-separated list, can be customized via environment variable
_default_blocked_content_types = (
    "application/x-executable,"
    "application/x-sh,"
    "application/x-bash,"
    "application/octet-stream,"
    "application/x-msdownload,"
    "application/x-msdos-program,"
    "application/x-binary,"
    "application/zip,"
    "application/x-tar,"
    "application/x-gzip,"
    "application/x-rar-compressed,"
    "application/x-7z-compressed"
)
HTTP_REQUEST_BLOCKED_CONTENT_TYPES = [
    ct.strip().lower()
    for ct in os.environ.get(
        "HTTP_REQUEST_BLOCKED_CONTENT_TYPES", _default_blocked_content_types
    ).split(",")
    if ct.strip()
]

# Maximum number of redirects to follow (0 to disable redirects)
HTTP_REQUEST_MAX_REDIRECTS = int(os.environ.get("HTTP_REQUEST_MAX_REDIRECTS", "3"))

# ---------------------------------------------------------------------------
# CRM outreach — walker + run retention (canon T20; the corpus names the
# mechanisms, these are the numbers). The walker's poll/batch come from the
# shared CRM_WORKER_INTERVAL / CRM_WORKER_BATCH above (worker-runtime.md).
# ---------------------------------------------------------------------------
# The claim pushes wake_at this far ahead: the timer IS the lease (canon T20).
CRM_WALKER_LEASE_SECONDS = int(os.environ.get("CRM_WALKER_LEASE_SECONDS", 300))

# Consecutive failed claims before a run parks for a human.
CRM_WALKER_MAX_ATTEMPTS = int(os.environ.get("CRM_WALKER_MAX_ATTEMPTS", 3))
# Exited runs age out (canon T20 exited_at: the retention sweep is most
# of what keeps the hot table small). Batched; leftovers go next tick.
CRM_RUN_RETENTION_DAYS = int(os.environ.get("CRM_RUN_RETENTION_DAYS", 90))
CRM_RUN_SWEEP_BATCH_SIZE = int(os.environ.get("CRM_RUN_SWEEP_BATCH_SIZE", 500))
# How often the walker pauses its claim to run one sweep pass.
CRM_RUN_SWEEP_INTERVAL_SECONDS = _positive_float(
    "CRM_RUN_SWEEP_INTERVAL_SECONDS", 3600.0
)
