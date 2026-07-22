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
        "model": "sonic-3.5",
        "speed": 1.0,
        "volume": 1.0,
        "emotion": "neutral",
        "language": "en",
    },
    "sarvam": {
        "voice_id": "shreya",
        "model": "bulbul:v3",
        "language": "en-IN",
        "speed": 0.9,
        "pitch": 0.0,
    },
    "soniox": {
        "voice_id": "Priya",
        "model": "tts-rt-v1",
        "language": "en",
    },
    # Google Cloud TTS — Chirp 3 HD voices. The voice name encodes the model
    # and locale (e.g. en-IN-Chirp3-HD-Despina), so there is no separate model
    # field; language should match the voice's locale prefix.
    "google": {
        "voice_id": "en-IN-Chirp3-HD-Despina",
        "language": "en-IN",
    },
    # DragonTTS caching proxy — model carries the nested provider as
    # "<provider>:<model>" (e.g. "cartesia:sonic-3.5").
    "dragontts": {
        "voice_id": "bec003e2-3cb3-429c-8468-206a393c67ad",
        "model": "cartesia:sonic-3.5",
        "language": "en",
    },
}


async def BREEZE_MCP_ENDPOINT_PATH() -> str:
    """Returns BREEZE_MCP_ENDPOINT_PATH from Redis"""
    return await get_config("BREEZE_MCP_ENDPOINT_PATH", "/ai/neurolink", str)


async def ENABLE_BACKGROUND_TASKS() -> bool:
    """Returns ENABLE_BACKGROUND_TASKS from Redis"""
    return await get_config("ENABLE_BACKGROUND_TASKS", "false", bool)


# ----------------------------------------------------------------------------
# Dispatcher dials. Re-read on every invocation, so DevCycle / Redis changes
# propagate without a pod restart. See docs/BACKLOG_DISPATCHER_REDESIGN.md
# §7.2 for alert wiring and §2 for the kill switch.
# ----------------------------------------------------------------------------


async def BB_DISPATCH_ENABLED() -> bool:
    """Global kill-switch for the dispatcher (default: True = enabled).

    When False, workers short-circuit their loop and stop consuming from
    the ready list — leads stay queued until the switch flips back on.
    Used for incident response; operators flip via DevCycle UI or by
    overriding ``BB_DISPATCH_ENABLED`` in the Redis feature-flag blob.
    """
    return await get_config("BB_DISPATCH_ENABLED", True, bool)


async def BB_SCHEDULE_DEPTH_ALERT_THRESHOLD() -> int:
    """ZCARD threshold above which `schedule_depth_high` Slack alert fires
    (`monitor_dispatch_health`). Raise during planned re-imports to silence
    alerts; lower to get earlier signal on ingest outpacing dispatch."""
    return await get_config("BB_SCHEDULE_DEPTH_ALERT_THRESHOLD", 50000, int)


async def BB_SCHEDULE_OVERDUE_ALERT_THRESHOLD() -> int:
    """Overdue (ZCOUNT 0..now) count above which `dispatch_halted` Slack
    alert fires when a leader is present. Tunes alert sensitivity without
    a redeploy."""
    return await get_config("BB_SCHEDULE_OVERDUE_ALERT_THRESHOLD", 100, int)


async def BB_CHANNEL_DRIFT_ALERT_THRESHOLD() -> int:
    """Per-number drift (|expected - actual| tokens) above which
    `channel_drift` Slack alert fires. Raise when provider webhooks are
    flaky in volume to suppress per-number noise."""
    return await get_config("BB_CHANNEL_DRIFT_ALERT_THRESHOLD", 5, int)


async def BB_STALE_LOCK_THRESHOLD_MINUTES() -> int:
    """Minutes a BACKLOG row may remain `is_locked=TRUE` before
    `clean_stale_bb_locks` unlocks it. Controls the §7.1 trade-off:
    lower = faster recovery, higher rare-duplicate rate; higher = stronger
    duplicate-call bound, slower recovery from worker crashes."""
    return await get_config("BB_STALE_LOCK_THRESHOLD_MINUTES", 10, int)


async def BB_RECONCILE_BACKLOG_LIMIT() -> int:
    """Maximum BACKLOG rows `reconcile_backlog_to_zset` ZADDs per tick.
    Crank up to drain a Redis-loss event faster; lower if the DB is under
    pressure and the scan is expensive."""
    return await get_config("BB_RECONCILE_BACKLOG_LIMIT", 1000, int)


async def BB_DAILY_BOT_SUBPROCESS() -> bool:
    """Run each Daily voice bot in its own OS subprocess (default: True).

    When True, ``start_daily_session`` spawns ``services/daily/bot_runner.py``
    per call, isolating the audio pipeline from API-traffic event-loop stalls
    (the widget voice crackle root cause) and containing daily-python native
    crashes to one call. Flip to False (DevCycle/Redis, or the
    ``BB_DAILY_BOT_SUBPROCESS`` env var) as the escape hatch back to the
    legacy in-process asyncio-task launch. Read at launch time only —
    flipping affects new calls, never live ones.
    """
    return await get_config("BB_DAILY_BOT_SUBPROCESS", True, bool)


async def BB_DAILY_AUDIO_OUT_10MS_CHUNKS() -> int:
    """Daily output write size in 10ms chunks (default: 10 = 100ms writes).

    Pipecat's ``audio_out_10ms_chunks`` (upstream default 4 = 40ms). Bigger
    chunks give the paced audio writer more event-loop slack against stalls in
    the bot process (ONNX turn detection, GC, LLM/tool work on the same loop)
    that would otherwise underrun Daily's virtual mic and crackle
    (pipecat#331). Trade-off: up to one chunk of trailing audio is dropped per
    utterance until pipecat ships the trailing-flush fix (pipecat#4993) — tune
    down (e.g. 6) via DevCycle/Redis or the env var to shave the tail clip
    once crackle is confirmed gone. Read when a bot builds its transport, so
    a change affects new calls only, never live ones.
    """
    return await get_config("BB_DAILY_AUDIO_OUT_10MS_CHUNKS", 10, int)


async def DAILY_SUMMARY_HOUR() -> int:
    """Returns DAILY_SUMMARY_HOUR from Redis (24-hour format: 0-23)"""
    return await get_config("DAILY_SUMMARY_HOUR", 21, int)


async def ENABLE_BREEZE_MCP() -> bool:
    """Returns ENABLE_BREEZE_MCP from Redis"""
    return await get_config("ENABLE_BREEZE_MCP", False, bool)


async def ENABLE_CHAT_MODE_PROMPT() -> bool:
    """Returns ENABLE_CHAT_MODE_PROMPT from Redis"""
    return await get_config("ENABLE_CHAT_MODE_PROMPT", True, bool)


# ============================================================================
# Chat (text-mode) idle session timeout. Read by the cleanup task in
# app/ai/voice/agents/breeze_buddy/chat/cleanup.py on every sweep so a
# DevCycle change propagates without a pod restart. The sweep cadence
# itself lives in static config (CHAT_SESSION_END_TIMEOUT_LOOP_INTERVAL_SECONDS
# in app/core/config/static.py) because the BackgroundTaskScheduler binds it
# once at startup.
# See docs/CHAT_MODE.md §7.3.
# ============================================================================
async def CHAT_SESSION_END_TIMEOUT_SECONDS() -> int:
    """Mark an ACTIVE/IDLE chat session ENDED once it has been inactive
    for this many seconds. The safety net that prevents zombie sessions
    from accumulating forever. Default 60 minutes."""
    return await get_config("CHAT_SESSION_END_TIMEOUT_SECONDS", 3600, int)


async def CHAT_HISTORY_REPLAY_LIMIT() -> int:
    """Cap on prior chat_message rows replayed into LLMContext per turn.

    The only per-turn read whose cost grows with conversation length;
    capping keeps DB read + LLM input-token cost flat for long
    sessions (and the LLM context window is bounded anyway). Default
    100 (≈50 user/assistant exchanges) — well above typical chat
    session length, so in practice the cap only kicks in for
    pathologically long sessions.

    Tuning trade-offs:
    - **Too low** (≤ ~10): the LLM "forgets" earlier context within
      a single session. Concrete failures — user states their name /
      account / preference early, asks about it 12 turns later, bot
      can't recall it; bot re-asks a question already answered;
      multi-step intents ("do X, then Y, then Z") lose the original
      intent once truncated; FlowManager node assumes context the
      LLM no longer sees, producing contradictions; previously-
      called function results drop out of context, so the LLM
      either re-calls (cost) or hallucinates.
    - **Too high** (≥ ~500): DB read returns a large rowset every
      turn; LLM input-token cost rises proportionally; TTFT latency
      grows with prompt length; risk of bumping the model's context
      window for very long sessions."""
    return await get_config("CHAT_HISTORY_REPLAY_LIMIT", 100, int)


async def WIDGET_STT_MAX_AUDIO_BYTES() -> int:
    """Max audio upload accepted by ``POST /widget/session/{id}/transcribe``
    (push-to-talk). Clips are short; the default 10 MB sits well under
    provider limits (OpenAI Whisper is 25 MB). Read per request so it can
    be tightened during abuse without a deploy."""
    return await get_config("WIDGET_STT_MAX_AUDIO_BYTES", 10 * 1024 * 1024, int)


async def STT_MAX_AUDIO_BYTES() -> int:
    """Max audio upload accepted by the standalone ``POST /stt/transcribe``
    endpoint (template-independent one-shot transcription). Same rationale as
    the widget limit: clips are short and the default 10 MB sits well under
    provider caps. Read per request so it can be tightened during abuse
    without a deploy."""
    return await get_config("STT_MAX_AUDIO_BYTES", 10 * 1024 * 1024, int)


async def SONIOX_ASYNC_MODEL() -> str:
    """Soniox async/file model for one-shot (push-to-talk) transcription.

    Used by ``transcribe_audio`` when a template's STT provider is Soniox.
    Resolves Redis → env → default, so the model can be bumped (e.g.
    ``stt-async-v5`` → ``v6``) without a deploy."""
    return await get_config("SONIOX_ASYNC_MODEL", "stt-async-v5", str)


# ============================================================================
# Public chat-demo tuning knobs (CHAT_MODE.md §13).
#
# All four are operational dials we may want to turn during incident
# response or after observing real demo traffic, *without* a deploy.
# Reads are async because they hit Redis/DevCycle — cheap, but call
# sites must ``await``. Each value is captured at the moment it's needed:
#
# - ``DEMO_MESSAGE_CAP_PER_SESSION`` is read at session-create and
#   baked into the demo JWT, so changes apply to *future* sessions only.
# - ``DEMO_TOKEN_TTL_MINUTES`` is read at mint time, same forward-only
#   semantics.
# - The two rate limits are read on every request, so they take effect
#   immediately on the next request.
# ============================================================================


async def DEMO_MESSAGE_CAP_PER_SESSION() -> int:
    """Hard ceiling on assistant turns per public demo session.

    Persisted into the demo JWT on session-create — the per-turn handler
    reads it from the token, not from Redis again, so changing this only
    affects sessions created *after* the tweak. Default 20.
    """
    return await get_config("DEMO_MESSAGE_CAP_PER_SESSION", 20, int)


async def DEMO_SESSIONS_PER_IP_HOUR() -> int:
    """Per-IP cap on demo session creates inside a 1-hour fixed window.
    Read on every ``POST /chat/demo/session`` so a tweak takes effect on
    the next request.

    Default 100 (intentionally loose). Real LLM spend is bounded by the
    *per-session* turn cap and the per-IP message rate — session creates
    are cheap (one DB insert + one greeting LLM call at most). Keeping
    this number in three digits avoids false positives when several
    visitors share a NAT'd IP (corporate networks, mobile carriers,
    classrooms doing a live demo)."""
    return await get_config("DEMO_SESSIONS_PER_IP_HOUR", 100, int)


async def DEMO_MESSAGES_PER_IP_HOUR() -> int:
    """Per-IP cap on demo messages inside a 1-hour fixed window. Same
    semantics as ``DEMO_SESSIONS_PER_IP_HOUR``. Default 600."""
    return await get_config("DEMO_MESSAGES_PER_IP_HOUR", 600, int)


async def DEMO_TOKEN_TTL_MINUTES() -> int:
    """Demo bearer-token lifetime, in minutes. Long enough for a typical
    demo conversation (cap + a few minutes of think time per turn) and
    short enough that a leaked token isn't a long-running attack vector.
    Read at mint time only. Default 30."""
    return await get_config("DEMO_TOKEN_TTL_MINUTES", 30, int)


# --- Sarvam Configuration ---
async def SARVAM_STT_MODEL() -> str:
    """Returns SARVAM_STT_MODEL from Redis"""
    return await get_config("SARVAM_STT_MODEL", "saaras:v3", str)


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
    return await get_config("BB_SARVAM_STT_MODEL", "saaras:v3", str)


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


async def GEMINI_TTS_MODEL() -> str:
    """Returns the default Gemini TTS model name from Redis.

    Override via Redis key GEMINI_TTS_MODEL.
    Default: gemini-3.1-flash-tts-preview
    """
    return await get_config("GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview", str)


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


async def BB_STRIP_EMOJIS_FROM_TTS() -> bool:
    """Whether to strip emoji from text sent to the TTS provider (default True).

    Applies to every voice flow (telephony, Daily, widget stream); no emoji is
    ever voiced. The widget stream-mode transcript is produced independently of
    TTS (bridge RTVI event + chat_message) and keeps its emoji; the telephony /
    Daily stored transcript reflects the spoken (emoji-free) text. Kill-switch.
    """
    return await get_config("BB_STRIP_EMOJIS_FROM_TTS", True, bool)


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


# --- OpenAI LLM Configuration ---
async def OPENAI_MAX_COMPLETION_TOKENS() -> int:
    """Returns OPENAI_MAX_COMPLETION_TOKENS from Redis"""
    return await get_config("OPENAI_MAX_COMPLETION_TOKENS", 300, int)


async def OPENAI_TEMPERATURE() -> float:
    """Returns OPENAI_TEMPERATURE from Redis"""
    return await get_config("OPENAI_TEMPERATURE", 0.7, float)


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


# --- Realtime / speech-to-speech LLM credentials ---
async def OPENAI_REALTIME_API_KEY() -> str:
    """Returns the OpenAI Realtime API key from Redis.

    Used by direct-mode templates with
    ``llm_configurations.realtime.provider="openai"``.
    """
    return await get_config("OPENAI_REALTIME_API_KEY", "", str)


async def XAI_REALTIME_API_KEY() -> str:
    """Returns the xAI Realtime API key from Redis.

    Used by direct-mode templates with
    ``llm_configurations.realtime.provider="xai"``.
    """
    return await get_config("XAI_REALTIME_API_KEY", "", str)


async def AZURE_OPENAI_REALTIME_API_KEY() -> str:
    """Returns the Azure OpenAI Realtime API key from Redis.

    Used by direct-mode templates with
    ``llm_configurations.realtime.provider="azure"``.
    """
    return await get_config("AZURE_OPENAI_REALTIME_API_KEY", "", str)


async def AZURE_OPENAI_REALTIME_ENDPOINT() -> str:
    """Returns the Azure OpenAI Realtime WebSocket endpoint URL from Redis.

    Must be the full Azure WebSocket URL including api-version and
    deployment, e.g. ``"wss://my-project.openai.azure.com/openai/realtime?
    api-version=2025-04-01-preview&deployment=my-realtime-deployment"``.
    Templates can override this per-call via
    ``llm_configurations.realtime.endpoint``.
    """
    return await get_config("AZURE_OPENAI_REALTIME_ENDPOINT", "", str)


# --- Knowledge Base (RAG) ---
async def KB_AZURE_OPENAI_ENDPOINT() -> str:
    """Azure endpoint for the ``azure_openai`` EMBEDDING provider (the
    default for new KBs). Deliberately separate from the LLM's static
    AZURE_OPENAI_ENDPOINT so repointing or rotating one never silently
    moves the other; Redis-settable at runtime, so the embed endpoint can
    be changed without a deploy."""
    return await get_config("KB_AZURE_OPENAI_ENDPOINT", "", str)


async def KB_AZURE_OPENAI_API_KEY() -> str:
    """API key for the ``azure_openai`` embedding provider — kept separate
    from the LLM's AZURE_OPENAI_API_KEY on purpose (see
    KB_AZURE_OPENAI_ENDPOINT); Redis-settable for no-deploy rotation."""
    return await get_config("KB_AZURE_OPENAI_API_KEY", "", str)


async def KB_INGEST_BATCH_SIZE() -> int:
    """Max documents the ingestion worker claims per tick/kick."""
    return await get_config("KB_INGEST_BATCH_SIZE", 5, int)


async def KB_INGESTION_INTERVAL_SECONDS() -> int:
    """Scheduler sweep interval for the KB ingestion task (read at startup).

    Hardened against malformed env overrides (get_env_value returns the RAW
    string when int() conversion fails): a bad value here would raise inside
    the startup try-block and silently disable ALL background tasks.
    """
    value = await get_config("KB_INGESTION_INTERVAL_SECONDS", 60, int)
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(
            f"Invalid KB_INGESTION_INTERVAL_SECONDS value {value!r}; using 60"
        )
        return 60


async def KB_STALE_PROCESSING_MINUTES() -> int:
    """Documents stuck in PROCESSING longer than this are requeued
    (crashed/redeployed worker pod)."""
    return await get_config("KB_STALE_PROCESSING_MINUTES", 15, int)


async def KB_MAX_FILE_MB() -> int:
    """Per-file upload size cap (also bounds parser memory/time)."""
    return await get_config("KB_MAX_FILE_MB", 20, int)


async def KB_MAX_DOCUMENTS_PER_KB() -> int:
    """Per-knowledge-base document count cap."""
    return await get_config("KB_MAX_DOCUMENTS_PER_KB", 100, int)


async def KB_MAX_CHUNKS_PER_KB() -> int:
    """Per-knowledge-base chunk cap (~40MB of text at 450-token chunks)."""
    return await get_config("KB_MAX_CHUNKS_PER_KB", 25000, int)


async def KB_MERCHANT_MAX_CHUNKS() -> int:
    """Per-reseller total chunk cap across all knowledge bases
    (noisy-neighbor protection for ingestion; raise per merchant on request).

    Default is sized to launch infra (~30MB of text / ~140MB of DB footprint
    per reseller on a 15GB instance); the per-KB cap above only binds once
    this one has been raised for a reseller."""
    return await get_config("KB_MERCHANT_MAX_CHUNKS", 20000, int)


# ----------------------------------------------------------------------------
# DragonTTS caching proxy + kill switch. Migrated from static env vars so the
# proxy URL / health-probe timeout / enable flag can be tuned live via Redis
# (devcycle:flags blob) without a redeploy. Resolution chain is the standard
# get_config one: Redis -> env -> the literal default below.
#
# (BACKGROUND_TASKS_LOOP_INTERVAL_SECONDS — the shared scheduler loop cadence —
# stays in static.py: it's a pre-existing knob for ALL background tasks and the
# scheduler binds it once at startup, so a Redis change would only take effect
# on the next pod restart anyway. DRAGONTTS_URL below is awaited on every call
# and every health probe.)
# ----------------------------------------------------------------------------


async def DRAGONTTS_URL() -> str:
    """Base URL of the DragonTTS caching proxy."""
    return await get_config(
        "DRAGONTTS_URL", "http://dragontts.beta.svc.cluster.local", str
    )


async def DRAGONTTS_HEALTH_TIMEOUT_S() -> float:
    """Health probe timeout in seconds (default 3.0). Defensive parse."""
    value = await get_config("DRAGONTTS_HEALTH_TIMEOUT_S", 3.0, float)
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning(f"Invalid DRAGONTTS_HEALTH_TIMEOUT_S value {value!r}; using 3.0")
        return 3.0


async def ENABLE_DRAGONTTS_KILL_SWITCH() -> bool:
    """Enable the DragonTTS health-monitor kill switch (default True)."""
    return await get_config("ENABLE_DRAGONTTS_KILL_SWITCH", True, bool)
