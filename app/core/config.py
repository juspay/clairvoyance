import os
from typing import List, Optional

from dotenv import load_dotenv
from loguru import logger

# --- Initial Setup ---
load_dotenv()


def _get_required_env(var_name: str) -> str:
    """A private helper to retrieve a required environment variable."""
    value = os.environ.get(var_name)
    if not value:
        error_msg = f"{var_name} environment variable is required"
        logger.error(error_msg)
        raise ValueError(error_msg)
    return value


class Config:
    """
    Centralized application config, loaded from environment variables.
    Configurations are grouped by functionality in nested classes.
    """

    class Server:
        """Uvicorn server settings."""

        PORT: int = int(os.environ.get("PORT", 8000))
        HOST: str = os.environ.get("HOST", "0.0.0.0")
        UVICORN_RELOAD: bool = (
            os.environ.get("UVICORN_RELOAD", "true").lower() == "true"
        )
        UVICORN_LOG_LEVEL: str = os.environ.get("UVICORN_LOG_LEVEL", "info")
        ENVIRONMENT: str = os.environ.get("ENVIRONMENT", "production")
        APP_BASE_URL: str = os.environ.get("APP_BASE_URL", "")
        CLOUD_ENVIRONMENT: str = os.environ.get("CLOUD_ENVIRONMENT", "GCP")

    class Logging:
        """Logging, tracing, and observability settings."""

        PROD_LOG_LEVEL: str = os.environ.get("PROD_LOG_LEVEL", "INFO")
        ENABLE_TRACING: bool = (
            os.environ.get("ENABLE_TRACING", "false").lower() == "true"
        )
        OPEN_OBSERVE_BASE_URL: str = os.environ.get(
            "OPEN_OBSERVE_BASE_URL", "https://periscope.breeze.in"
        )

    class Langfuse:
        ENABLE_LANGFUSE_PROMPTS: bool = (
            os.environ.get("ENABLE_LANGFUSE_PROMPTS", "false").lower() == "true"
        )
        LANGFUSE_SECRET_KEY: str = os.environ.get("LANGFUSE_SECRET_KEY", "")
        LANGFUSE_PUBLIC_KEY: str = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        LANGFUSE_BASEURL: str = os.environ.get(
            "LANGFUSE_BASEURL", "https://us.cloud.langfuse.com"
        )
        AUTOMATIC_LANGFUSE_PROMPT_NAME: str = os.environ.get(
            "AUTOMATIC_LANGFUSE_PROMPT_NAME", "AUTOMATIC_VOICE_LANGFUSE_PROMPT"
        )
        AUTOMATIC_LANGFUSE_SYSTEM_PROMPT_LABEL: str = os.environ.get(
            "AUTOMATIC_LANGFUSE_SYSTEM_PROMPT_LABEL", "automatic_system_langfuse_prompt"
        )

    class AI:
        """Core AI and Large Language Model (LLM) settings."""

        GEMINI_API_KEY: str = _get_required_env("GEMINI_API_KEY")
        ENABLE_SEARCH_GROUNDING: bool = (
            os.environ.get("ENABLE_SEARCH_GROUNDING", "true").lower() == "true"
        )
        GEMINI_SEARCH_RESULT_API_MODEL: str = os.environ.get(
            "GEMINI_SEARCH_RESULT_API_MODEL", "gemini-2.5-flash-lite-preview-06-17"
        )
        GOOGLE_CREDENTIALS_JSON: str = _get_required_env("GOOGLE_CREDENTIALS_JSON")
        AZURE_OPENAI_API_KEY: str = _get_required_env("AZURE_OPENAI_API_KEY")
        AZURE_OPENAI_ENDPOINT: str = _get_required_env("AZURE_OPENAI_ENDPOINT")
        AZURE_OPENAI_MODEL: str = os.environ.get(
            "AZURE_OPENAI_MODEL", "gpt-4o-automatic"
        )

    class Daily:
        """Daily framework settings."""

        DAILY_API_KEY: str = _get_required_env("DAILY_API_KEY")
        DAILY_API_URL: str = os.environ.get("DAILY_API_URL", "https://api.daily.co/v1")
        MAX_DAILY_SESSION_LIMIT: int = int(
            os.environ.get("MAX_DAILY_SESSION_LIMIT", 1800)
        )
        ENABLE_AUTOMATIC_DAILY_RECORDING: bool = (
            os.environ.get("ENABLE_AUTOMATIC_DAILY_RECORDING", "false").lower()
            == "true"
        )

    class STT:
        """Speech-to-Text (STT) provider settings."""

        STT_PROVIDER: str = os.environ.get("STT_PROVIDER", "google").lower()

        # OpenAI
        OPENAI_STT_API_KEY: Optional[str] = os.getenv("OPENAI_STT_API_KEY")
        OPENAI_STT_MODEL: str = os.environ.get("OPENAI_STT_MODEL", "gpt-4o-transcribe")
        ENFORCED_OPENAI_STT_MODEL: str = os.environ.get(
            "ENFORCED_OPENAI_STT_MODEL", "whisper-1"
        )
        AUTOMATIC_OPENAI_STT_PROMPT: str = os.environ.get(
            "AUTOMATIC_OPENAI_STT_PROMPT", ""
        )

        ENABLE_OPENAI_FOR_MIA: bool = (
            os.environ.get("ENABLE_OPENAI_FOR_MIA", "false").lower() == "true"
        )

        # AssemblyAI
        ASSEMBLYAI_API_KEY: Optional[str] = os.getenv("ASSEMBLYAI_API_KEY")

        # Deepgram
        DEEPGRAM_API_KEY: Optional[str] = os.getenv("DEEPGRAM_API_KEY")
        DEEPGRAM_MODEL: str = os.environ.get("DEEPGRAM_MODEL", "nova-3-general")
        DEEPGRAM_LANGUAGE: str = os.environ.get("DEEPGRAM_LANGUAGE", "en")
        DEEPGRAM_ENDPOINTING: bool = (
            os.environ.get("DEEPGRAM_ENDPOINTING", "true").lower() == "true"
        )
        DEEPGRAM_VAD_EVENTS: bool = (
            os.environ.get("DEEPGRAM_VAD_EVENTS", "true").lower() == "true"
        )
        DEEPGRAM_UTTERANCE_END_MS: int = int(
            os.environ.get("DEEPGRAM_UTTERANCE_END_MS", "1000")
        )
        DEEPGRAM_NO_DELAY: bool = (
            os.environ.get("DEEPGRAM_NO_DELAY", "true").lower() == "true"
        )
        DEEPGRAM_SMART_FORMAT: bool = (
            os.environ.get("DEEPGRAM_SMART_FORMAT", "true").lower() == "true"
        )
        DEEPGRAM_PUNCTUATE: bool = (
            os.environ.get("DEEPGRAM_PUNCTUATE", "true").lower() == "true"
        )
        DEEPGRAM_NUMERALS: bool = (
            os.environ.get("DEEPGRAM_NUMERALS", "true").lower() == "true"
        )
        DEEPGRAM_PROFANITY_FILTER: bool = (
            os.environ.get("DEEPGRAM_PROFANITY_FILTER", "false").lower() == "true"
        )
        DEEPGRAM_DIARIZE: bool = (
            os.environ.get("DEEPGRAM_DIARIZE", "false").lower() == "true"
        )
        DEEPGRAM_AUTO_DETECT_LANGUAGE: bool = (
            os.environ.get("DEEPGRAM_AUTO_DETECT_LANGUAGE", "false").lower() == "true"
        )

        # Soniox
        SONIOX_API_KEY: Optional[str] = os.getenv("SONIOX_API_KEY")
        SONIOX_MODEL: str = os.environ.get("SONIOX_MODEL", "stt-rt-preview")
        SONIOX_LANGUAGE_HINTS: str = os.environ.get("SONIOX_LANGUAGE_HINTS", "en")
        SONIOX_CONTEXT: str = os.environ.get(
            "SONIOX_CONTEXT",
            "PSR, GMV, UPI, ROAS, AOV, RTO, COD, Sales, Cart, Abandonment, Sales, Split, What",
        )
        SONIOX_ENABLE_NON_FINAL_TOKENS: bool = (
            os.environ.get("SONIOX_ENABLE_NON_FINAL_TOKENS", "false").lower() == "true"
        )
        SONIOX_VAD_FORCE_TURN_ENDPOINT: bool = (
            os.environ.get("SONIOX_VAD_FORCE_TURN_ENDPOINT", "false").lower() == "true"
        )
        SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS = int(
            os.environ.get("SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS", "0")
        )

    class TTS:
        """Text-to-Speech (TTS) provider settings."""

        SANITIZE_TEXT_FOR_TTS: bool = (
            os.environ.get("SANITIZE_TEXT_FOR_TTS", "false").lower() == "true"
        )

        # ElevenLabs
        ELEVENLABS_API_KEY: Optional[str] = os.environ.get("ELEVENLABS_API_KEY")
        ELEVENLABS_VOICE_ID: str = os.environ.get(
            "ELEVENLABS_VOICE_ID", "bQQWtYx9EodAqMdkrNAc"
        )
        ELEVENLABS_RHEA_VOICE_ID: str = os.environ.get(
            "ELEVENLABS_RHEA_VOICE_ID", "bQQWtYx9EodAqMdkrNAc"
        )
        ELEVENLABS_BB_VOICE_ID: str = os.environ.get(
            "ELEVENLABS_BB_VOICE_ID", "fG9s0SXJb213f4UxVHyG"
        )
        ELEVENLABS_MODEL_ID: str = os.environ.get(
            "ELEVENLABS_MODEL_ID", "eleven_flash_v2_5"
        )
        ELEVENLABS_VOICE_SPEED: float = float(
            os.environ.get("ELEVENLABS_VOICE_SPEED", 1.15)
        )
        ELEVENLABS_TTS_SPEED: float = float(
            os.environ.get("ELEVENLABS_TTS_SPEED", "1.10")
        )

        # Google
        GOOGLE_BRET_VOICE: str = os.environ.get(
            "GOOGLE_BRET_VOICE", "en-IN-Chirp3-HD-Sadaltager"
        )
        GOOGLE_MIA_VOICE: str = os.environ.get(
            "GOOGLE_MIA_VOICE", "en-IN-Chirp3-HD-Despina"
        )

    class Audio:
        """Audio processing, VAD, and filtering settings."""

        SAMPLE_RATE: int = 16000
        FRAME_DURATION: int = 30  # ms
        FRAME_SIZE: int = int(SAMPLE_RATE * FRAME_DURATION / 1000) * 2

        # VAD & Turn Management
        VAD_CONFIDENCE: float = float(os.environ.get("VAD_CONFIDENCE", 0.85))
        VAD_MIN_VOLUME: float = float(os.environ.get("VAD_MIN_VOLUME", 0.75))
        VAD_START_SECS: float = float(os.environ.get("VAD_START_SECS", 0.30))
        VAD_STOP_SECS: float = float(os.environ.get("VAD_STOP_SECS", 1.00))
        DISABLE_SILERO_VAD: bool = (
            os.environ.get("DISABLE_SILERO_VAD", "false").lower() == "true"
        )
        DISABLE_VAD_FOR_PTT: bool = (
            os.environ.get("DISABLE_VAD_FOR_PTT", "true").lower() == "true"
        )

        # Filters
        ENABLE_NOISE_REDUCE_FILTER: bool = (
            os.environ.get("ENABLE_NOISE_REDUCE_FILTER", "true").lower() == "true"
        )
        ENABLE_AIC_FILTER: bool = (
            os.environ.get("ENABLE_AIC_FILTER", "false").lower() == "true"
        )
        AICOUSTICS_LICENSE_KEY: str = os.environ.get("AICOUSTICS_LICENSE_KEY", "")
        AIC_ENHANCEMENT_LEVEL: float = float(
            os.environ.get("AIC_ENHANCEMENT_LEVEL", "1.0")
        )
        AIC_VOICE_GAIN: float = float(os.environ.get("AIC_VOICE_GAIN", "1.2"))
        AIC_NOISE_GATE_ENABLE: bool = (
            os.environ.get("AIC_NOISE_GATE_ENABLE", "true").lower() == "true"
        )
        ENABLE_KRISP_FILTER: bool = (
            os.environ.get("ENABLE_KRISP_FILTER", "false").lower() == "true"
        )
        KRISP_MODEL_PATH: str = os.environ.get(
            "KRISP_MODEL_PATH", "/app/models/voice/krisp/krisp-viva-tel-v2.kef"
        )

    class BreezeBuddy:
        """Specific settings for the 'Breeze Buddy' persona/agent."""

        # VAD
        BREEZE_BUDDY_VAD_CONFIDENCE: float = float(
            os.getenv("BREEZE_BUDDY_VAD_CONFIDENCE", 0.7)
        )
        BREEZE_BUDDY_VAD_START_SECS: float = float(
            os.getenv("BREEZE_BUDDY_VAD_START_SECS", 0.2)
        )
        BREEZE_BUDDY_VAD_STOP_SECS: float = float(
            os.getenv("BREEZE_BUDDY_VAD_STOP_SECS", 0.8)
        )
        BREEZE_BUDDY_VAD_MIN_VOLUME: float = float(
            os.getenv("BREEZE_BUDDY_VAD_MIN_VOLUME", 0.6)
        )

        # STT
        BREEZE_BUDDY_STT_SERVICE: str = os.getenv(
            "BREEZE_BUDDY_STT_SERVICE", "soniox"
        ).lower()

        # Soniox specific for Breeze Buddy
        BREEZE_BUDDY_SONIOX_MODEL: str = os.environ.get(
            "BREEZE_BUDDY_SONIOX_MODEL", "stt-rt-preview"
        )
        BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS: str = os.environ.get(
            "BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS", "en,hi"
        )
        BREEZE_BUDDY_SONIOX_CONTEXT: str = os.environ.get(
            "BREEZE_BUDDY_SONIOX_CONTEXT",
            "State, Yes, Yeah, Good, Time, Yep, Later, Available, Busy, Confirm, Cancel, Repeat",
        )
        BREEZE_BUDDY_SONIOX_ENABLE_NON_FINAL_TOKENS: bool = (
            os.environ.get(
                "BREEZE_BUDDY_SONIOX_ENABLE_NON_FINAL_TOKENS", "false"
            ).lower()
            == "true"
        )
        BREEZE_BUDDY_SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS: int = int(
            os.environ.get("BREEZE_BUDDY_SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS", "0")
        )
        BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT: bool = (
            os.environ.get(
                "BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT", "false"
            ).lower()
            == "true"
        )
        AZURE_BREEZE_BUDDY_OPENAI_MODEL: str = os.environ.get(
            "AZURE_BREEZE_BUDDY_OPENAI_MODEL", "gpt-4o-automatic"
        )

    class Session:
        """Conversation, memory, and session management settings."""

        # Mem0
        MEM0_API_KEY: str = os.getenv("MEM0_API_KEY", "")
        MEM0_ENABLED: bool = os.getenv("MEM0_ENABLED", "false").lower() == "true"
        MEM0_MAX_FAILURES: int = int(os.getenv("MEM0_MAX_FAILURES", "3"))
        MEM0_RETRY_INTERVAL: int = int(os.getenv("MEM0_RETRY_INTERVAL", "300"))
        MEM0_SESSION_TIMEOUT: int = int(os.getenv("MEM0_SESSION_TIMEOUT", "3600"))
        MEM0_MIN_MESSAGE_LENGTH: int = int(os.getenv("MEM0_MIN_MESSAGE_LENGTH", "10"))

        # Context Summarization
        ENABLE_SUMMARIZATION: bool = (
            os.environ.get("ENABLE_SUMMARIZATION", "true").lower() == "true"
        )
        MAX_TURNS_BEFORE_SUMMARY: int = int(
            os.environ.get("MAX_TURNS_BEFORE_SUMMARY", 10)
        )
        KEEP_RECENT_TURNS: int = int(os.environ.get("KEEP_RECENT_TURNS", 2))

        # Timeouts
        AUTOMATIC_SESSION_INACTIVITY_TIMEOUT: float = float(
            os.environ.get("AUTOMATIC_SESSION_INACTIVITY_TIMEOUT", 900.0)
        )

    class HITL:
        """Human-in-the-Loop (HITL) settings."""

        HITL_ENABLE: bool = os.environ.get("HITL_ENABLE", "true").lower() == "true"
        FUNCTION_CONFIRMATION_TIMEOUT: int = int(
            os.environ.get("FUNCTION_CONFIRMATION_TIMEOUT", "30")
        )
        _actions_str = os.environ.get("HITL_ACTIONS", "delete")
        HITL_ACTIONS: List[str] = [
            action.strip().lower()
            for action in _actions_str.split(",")
            if action.strip()
        ]

    class Tools:
        """Settings for external tools, function calling, and features."""

        ENABLE_CHARTS: bool = os.environ.get("ENABLE_CHARTS", "false").lower() == "true"
        MAX_CHARTS_PER_TURN = int(os.environ.get("MAX_CHARTS_PER_TURN", "1"))
        ENABLE_ALL_METRICS_FROM_CKH: bool = (
            os.environ.get("ENABLE_ALL_METRICS_FROM_CKH", "true").lower() == "true"
        )
        BREEZE_DEFAULT_SALES_TAB: str = os.environ.get(
            "BREEZE_DEFAULT_SALES_TAB", "SALES"
        )
        MCP_CLIENT_TIMEOUT: int = int(os.environ.get("MCP_CLIENT_TIMEOUT", 30))
        ENABLE_TOOL_CALL_SOUND = (
            os.environ.get("ENABLE_TOOL_CALL_SOUND", "false").lower() == "true"
        )
        TOOL_CALL_SOUND_FILE = os.environ.get(
            "TOOL_CALL_SOUND_FILE", "assets/sounds/think.wav"
        )

        # Breeze MCP
        ENABLE_BREEZE_MCP: bool = (
            os.environ.get("ENABLE_BREEZE_MCP", "false").lower() == "true"
        )
        BREEZE_MCP_ENDPOINT_PATH: str = os.environ.get(
            "BREEZE_MCP_ENDPOINT_PATH", "/ai/neurolink"
        )
        _shops_for_mcp = os.environ.get("SHOPS_FOR_BREEZE_MCP", "")
        SHOPS_FOR_BREEZE_MCP: List[str] = [
            shop.strip() for shop in _shops_for_mcp.split(",") if shop.strip()
        ]

    class ExternalAPIs:
        """Credentials and URLs for external services."""

        # Juspay
        GENIUS_API_URL: str = (
            "https://portal.juspay.in/api/q/query?api-type=genius-query"
        )
        EULER_DASHBOARD_API_URL: str = os.environ.get(
            "EULER_DASHBOARD_API_URL", "https://portal.juspay.in"
        )

        # Twilio
        TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
        TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
        TWILIO_WEBSOCKET_URL: str = os.getenv("TWILIO_WEBSOCKET_URL", "")

        # Exotel
        EXOTEL_ACCOUNT_SID: str = os.getenv("EXOTEL_ACCOUNT_SID", "")
        EXOTEL_API_KEY: str = os.getenv("EXOTEL_API_KEY", "")
        EXOTEL_API_TOKEN: str = os.getenv("EXOTEL_API_TOKEN", "")
        EXOTEL_SUBDOMAIN: str = os.getenv("EXOTEL_SUBDOMAIN", "api.exotel.com")
        EXOTEL_APPLET_APP_ID: str = os.getenv("EXOTEL_APPLET_APP_ID", "1044183")

        # Breeze Portal
        LIGHTHOUSE_APP_URL: str = os.environ.get(
            "LIGHTHOUSE_APP_URL", "http://localhost:5173"
        )
        AWS_BREEZE_PORTAL_URL: str = os.environ.get(
            "AWS_BREEZE_PORTAL_URL", "https://portal.breeze.in"
        )
        GCP_BREEZE_PORTAL_URL: str = os.environ.get(
            "GCP_BREEZE_PORTAL_URL", "https://portal.breezesdk.store"
        )

    class Database:
        """PostgreSQL database and connection pool settings."""

        POSTGRES_USER: str = os.getenv("POSTGRES_USER", "")
        POSTGRES_PASSWORD: str = os.getenv("POSTGRES_PASSWORD", "")
        POSTGRES_HOST: str = os.getenv("POSTGRES_HOST", "")
        POSTGRES_PORT: str = os.getenv("POSTGRES_PORT", "")
        POSTGRES_DB: str = os.getenv("POSTGRES_DB", "")
        POSTGRES_POOL_SIZE: int = int(os.getenv("POSTGRES_POOL_SIZE", "5"))
        POSTGRES_MAX_OVERFLOW: int = int(os.getenv("POSTGRES_MAX_OVERFLOW", "10"))
        POSTGRES_POOL_RECYCLE: int = int(os.getenv("POSTGRES_POOL_RECYCLE", "3600"))

    class Security:
        """Settings for authentication, authorization, and encryption."""

        # JWT
        JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "")
        JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "")
        JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = int(
            os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "60")
        )

        # AWS KMS
        SKIP_KMS_DECRYPT: bool = (
            os.getenv("SKIP_KMS_DECRYPT", "false").lower() == "true"
        )
        AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")
        AWS_ACCESS_KEY_ID: str = os.getenv("AWS_ACCESS_KEY_ID", "")
        AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")

        # Webhook
        ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY: str = os.getenv(
            "ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY", ""
        )
        ORDER_CONFIRMATION_TOKEN: str = os.getenv("ORDER_CONFIRMATION_TOKEN", "")

        # User & Action Authorization
        _auth_users_str = os.environ.get("AUTOMATIC_WRITE_ACTIONS_AUTHORIZED_USERS", "")
        AUTOMATIC_WRITE_ACTIONS_AUTHORIZED_USERS: List[str] = [
            email.strip().lower()
            for email in _auth_users_str.split(",")
            if email.strip()
        ]
        _auth_actions_str = os.environ.get("AUTOMATIC_ACTIONS_REQUIRE_AUTH", "")
        AUTOMATIC_ACTIONS_REQUIRE_AUTH: List[str] = [
            action.strip().lower()
            for action in _auth_actions_str.split(",")
            if action.strip()
        ]
        LIGHTHOUSE_JWT_SECRET = os.getenv("LIGHTHOUSE_JWT_SECRET", "")
        ENABLE_LIGHTHOUSE_AUTH = (
            os.getenv("ENABLE_LIGHTHOUSE_AUTH", "false").lower() == "true"
        )

    class Network:
        """Network settings for WebSockets and proxies."""

        AWS_PROXY_HOST: Optional[str] = os.environ.get("AWS_PROXY_HOST")
        AWS_PROXY_PORT: Optional[str] = os.environ.get("AWS_PROXY_PORT")


# --- Singleton Instance ---
config = Config()
