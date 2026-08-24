"""Application configuration — env-backed settings + provider defaults.

Provider API-key env names mirror clairvoyance so the same k8s Secret can be
reused across both services.
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Per-provider defaults. Ported from clairvoyance BB_SPEECH_PROVIDER_DEFAULTS.
# Used as fallbacks when a request omits a field, and as the canonicalization
# baseline for cache-key collapsing (see app/cache/key.py).
PROVIDER_DEFAULTS: dict[str, dict] = {
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
        "speed": 1.0,
        "pitch": 0.0,
    },
    "elevenlabs": {
        "voice_id": "fG9s0SXJb213f4UxVHyG",
        "model": "eleven_flash_v2_5",
        "speed": 1.0,
        "language": "en",
        # SSML off by default. Listed here so canonical_params collapses an
        # explicit False with "absent" into ONE cache key (both = plain-text
        # synth); only enable_ssml_parsing=True gets its own key. ElevenLabs
        # only honors this on eleven_flash_v2_5 etc. (NOT eleven_v3).
        "enable_ssml_parsing": False,
    },
    "gemini": {
        "voice_id": "Kore",
        "model": "gemini-3.1-flash-tts-preview",
        "language": "en-IN",
    },
}

SUPPORTED_PROVIDERS = tuple(PROVIDER_DEFAULTS.keys())

# Indian-residency ElevenLabs host (matches clairvoyance's residency endpoint).
# The residency KEY is the only thing that must be supplied; the URL defaults
# here and is coerced non-empty by the validator below even if .env blanks it.
ELEVENLABS_INDIAN_RESIDENCY_URL = "https://api.in.residency.elevenlabs.io"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Provider credentials (env names mirror clairvoyance) ---
    cartesia_api_key: str = ""
    sarvam_api_key: str = ""
    # ElevenLabs runs ONLY against the Indian-residency endpoint (the only creds
    # with API access). Env names mirror clairvoyance's residency Secret keys so
    # the same k8s Secret is reused; the single "elevenlabs" route resolves here.
    elevenlabs_indian_residency_api_key: str = ""
    elevenlabs_indian_residency_base_url: str = ELEVENLABS_INDIAN_RESIDENCY_URL

    @field_validator("elevenlabs_indian_residency_base_url", mode="after")
    @classmethod
    def _coerce_residency_url(cls, v: str) -> str:
        # An empty ELEVENLABS_INDIAN_RESIDENCY_BASE_URL= line in .env would
        # otherwise blank the default; fall back to the known residency host.
        return v or ELEVENLABS_INDIAN_RESIDENCY_URL

    google_credentials_json: str = ""
    google_credentials_path: str = ""

    # --- Storage ---
    db_path: str = "data/dragontts.db"
    blob_dir: str = "data/blobs"

    # --- Cache policy ---
    max_cache_bytes: int = 0  # 0 = unlimited
    enable_write_through: bool = True
    # --- Length-scaled TTL (applied to EVERY stored entry: write-through).
    # ttl_for(words) = clamp(BASE + PER_WORD*words, BASE, MAX), so
    # longer phrases survive longer (more synth savings to preserve) and short
    # ones age out faster. There is no permanent tier: the periodic purge job
    # (TTL_PURGE_INTERVAL_SECONDS) deletes expired rows + blobs. Set BASE<=0 to
    # disable TTL entirely (entries never expire, no purge). Supersedes the flat
    # ttl_seconds knob below, which is kept only for back-compat.
    cache_ttl_base_seconds: int = 300  # 5 min floor — min TTL for any phrase
    cache_ttl_per_word_seconds: int = 300  # +5 min per word (10 words ≈ 55 min)
    cache_ttl_max_seconds: int = 518400  # 6d cap
    # Purge schedule: fire at ttl_purge_window_start_hour in ttl_purge_window_tz
    # (02:00 IST = low-traffic) and SKIP the window unless
    # ttl_purge_every_days elapsed since the last run. <=0 disables scheduling
    # and falls back to the legacy fixed-interval sweep below.
    ttl_purge_every_days: int = 2
    ttl_purge_window_start_hour: int = 2
    ttl_purge_window_tz: str = "Asia/Kolkata"
    ttl_purge_interval_seconds: int = 1200  # legacy sweep cadence (fallback only)

    @field_validator("ttl_purge_window_start_hour")
    @classmethod
    def _validate_purge_hour(cls, v: int) -> int:
        # An out-of-range hour would make datetime.replace() raise inside the
        # purge loop and silently stop TTL purging altogether.
        if not 0 <= v <= 23:
            raise ValueError("ttl_purge_window_start_hour must be 0-23")
        return v

    # Backfill for pre-existing entries (ttl_expires_at IS NULL, e.g. created
    # before this feature under ttl_seconds=0). At startup each NULL row gets a
    # RANDOM expiry in [min,max] hours so they age out gradually instead of
    # becoming permanent — and randomized (not flat) to avoid a synchronized
    # re-synth spike. Idempotent: only touches NULL rows.
    cache_ttl_backfill_min_hours: int = 48
    cache_ttl_backfill_max_hours: int = 72
    ttl_seconds: int = 0  # legacy flat TTL; unused now (length-scaled TTL supersedes)
    # --- TTS text normalization ---
    # Expand standalone numbers to English words (Indian grouping) before synth,
    # applied to both the synth text and the cache key (so "599" and
    # "5 hundred 99" share one entry). ElevenLabs garbles digit+word hybrids
    # ("5 hundred 99" -> "finee hundres"); expanding fixes it.
    tts_normalize_numbers: bool = False
    # ElevenLabs only: when true, ensure the text starts with "." — prepend one
    # (no space) if missing, leave it if already present (helps ElevenLabs read
    # digit+word hybrids / number-first openings cleanly). On by default; opt out
    # with TTS_LEADING_DOT=false. Applies to EVERY ElevenLabs synth (split or
    # not); other providers are unaffected (they read a leading "." fine).
    tts_leading_dot: bool = True
    # Split an incoming transcript into sentences BEFORE synth/serve, so each
    # sentence is synthesized + cached under its own key (reusable by any later
    # request that sends that sentence alone — e.g. recurring greetings). Set to
    # the symbols to split AFTER, e.g. ".:?!" — the split fires at whitespace
    # following one of these, so decimals ("3.14") and URLs are not broken. Empty
    # (default) = OFF -> the whole transcript is one entry (today's behavior).
    # The synth and stream paths are controlled INDEPENDENTLY (each can be off, or
    # use a different symbol set):
    split_at_symbols: str = ""  # /tts/bytes  (one-shot synth) path
    split_at_symbols_stream: str = ""  # /tts/stream path
    # PER-PART min-words gate: a part with fewer words is too small to be worth
    # its own cache entry, so consecutive short parts are merged into one — but a
    # long sentence is never merged with a short neighbour. So one short sentence
    # ("OK.") no longer collapses the whole phrase: the long sentences keep their
    # own keys. Default 2 = "more than 1 word". Set to 1 to split even
    # single-word sentences.
    split_min_words_per_part: int = 2
    # Synthesize the split parts CONCURRENTLY (asyncio.gather) instead of one at
    # a time. Off by default — the sequential path is the proven one; opt in only
    # once you've confirmed concurrent provider calls are safe for your workload
    # (single-flight already de-dupes identical keys). Applies to both the
    # /tts/bytes and /tts/stream split paths. When ON, the /tts/stream split
    # header is derived from each part's TRUE status (no stale metadata probe);
    # when OFF it stays a best-effort probe that may say HIT on a TOCTOU miss.
    split_parallel: bool = False
    # Split parts are pure-concatenated (no edge trim, no inserted gap) — nothing
    # is cut and each part is appended exactly as synthesized. There is no gap
    # knob: the provider's own leading/trailing silence between parts is left
    # intact. (If you later want a normalized inter-sentence gap, re-introduce it
    # in _get_or_synthesize_split / _stream_split.)

    # --- Performance ---
    thread_pool_workers: int = 32  # asyncio.to_thread pool size
    bulk_create_max: int = 100  # hard cap on /tts/create/bulk items
    # --- Memory: return freed glibc heap to the OS (RSS-creep mitigation) ---
    # glibc keeps freed memory in its arenas instead of releasing it to the OS,
    # so the large short-lived audio + numpy resample buffers fragment the heap
    # and RSS climbs to a high plateau (especially across 4 workers). malloc_trim
    # hands that freed memory back to the OS. Safe + idempotent (a no-op when
    # nothing is trimmable), and it has NO effect on audio quality or concurrency
    # — it only releases already-freed heap; it changes no workers/pools/caps.
    # gc.collect() first drops unreachable Python objects that may pin C buffers.
    # Runs once per worker process; skipped silently on non-glibc (musl) where
    # libc.so.6 / malloc_trim is absent. Default on, every 5 min.
    malloc_trim_enabled: bool = True
    malloc_trim_interval_seconds: int = 300
    # Number of warm, persistent Cartesia streaming sockets kept ready for cache
    # misses (each multiplexes many utterances by context_id). Set via env, e.g.
    # CARTESIA_STREAM_POOL_SIZE=4. 0 => open a fresh socket per miss (no pooling).
    cartesia_stream_pool_size: int = 3
    # Warm ElevenLabs multi-context WS sockets, pooled PER VOICE (the voice is in
    # the WS URL). Each socket multiplexes up to 5 concurrent contexts. Streaming
    # misses reuse a warm socket; if none is ready, fall back to one-shot HTTP.
    elevenlabs_stream_pool_size: int = 16
    # Max server-silence gap (seconds) after audio starts that ends an ElevenLabs
    # WS utterance (ElevenLabs delays is_final ~20s). Lower = faster stream
    # close/turn-end; raise if long utterances ever truncate at a >N s pause.
    elevenlabs_stream_idle_timeout: float = 0.8
    # Warm Sarvam WS sockets. Sarvam is NOT multiplexed (one utterance per socket
    # at a time), so this is a LIFO stack of warm, pre-configured connections. 0
    # => stream via a fresh socket per miss (no pooling).
    sarvam_stream_pool_size: int = 3
    # Force IPv4 for the Cartesia WS handshake. Some networks advertise IPv6
    # (AAAA) for api.cartesia.ai but black-hole the SYN, hanging the handshake
    # while IPv4 works fine. Safe default (IPv4 always reaches Cartesia).
    cartesia_ws_force_ipv4: bool = True
    # --- Resilience: per-provider bulkhead (concurrency cap) + rate limit ---
    # Caps how many in-flight synth/stream calls ONE provider may have so a slow
    # or hung provider can't exhaust shared resources (event loop, worker pool,
    # memory) and starve the others. Global defaults; override per provider via
    # the JSON env PROVIDER_RESILIENCE='{"cartesia":{"max_concurrent":20,
    # "rate_per_sec":8,"wait_timeout_ms":2000}, ...}'. A 0 value disables that
    # limiter. The bulkhead waits up to wait_timeout_ms for a slot, else 503.
    provider_max_concurrent_synths: int = 24
    provider_rate_limit_per_sec: float = 0.0
    provider_bulkhead_wait_timeout_ms: int = 2500
    provider_resilience_overrides: Dict[str, Dict[str, Any]] = Field(
        default_factory=lambda: {
            "cartesia": {"max_concurrent": 10, "wait_timeout_ms": 3000},
            "sarvam": {"max_concurrent": 10, "wait_timeout_ms": 3000},
            "elevenlabs": {"max_concurrent": 64, "wait_timeout_ms": 5000},
            "gemini": {"max_concurrent": 64, "wait_timeout_ms": 5000},
        },
        # The documented + deployed env name is PROVIDER_RESILIENCE — without
        # this alias pydantic only matches PROVIDER_RESILIENCE_OVERRIDES and a
        # deployment setting the documented name is SILENTLY ignored
        # (extra="ignore"), leaving the bulkhead at the global defaults. Accept
        # both names.
        validation_alias=AliasChoices(
            "PROVIDER_RESILIENCE", "PROVIDER_RESILIENCE_OVERRIDES"
        ),
    )
    # --- Write-behind metrics (off the hot HIT path) ---
    # HIT touch/metric updates are batched + flushed by a background task so a HIT
    # returns audio without awaiting a SQLite write. Flush by interval or batch
    # size, and on graceful shutdown. enabled=false -> synchronous writes.
    metrics_write_behind_enabled: bool = True
    metrics_flush_interval_ms: int = 500
    metrics_flush_batch_size: int = 64
    # Latency sampling: fraction of requests timed for the avg/p95 rollup (0
    # disables). perf_counter is cheap; sampling bounds latency_samples growth.
    metrics_latency_sample_rate: float = (
        0.3  # fraction of requests timed for the latency rollup
    )
    # latency_samples rows older than this are pruned by the periodic checkpoint
    # loop, keeping the table bounded.
    metrics_latency_retention_days: int = 14
    # Max width (days) of any analytics date range (/stats, /stats/daily,
    # /stats/latency). Bounds the aggregation so a wide "last year" range can't
    # CPU-bomb the single pod; unset from/to defaults to the last N days (not
    # all-time, which would full-scan metrics_daily/latency_samples). Raise for
    # longer cache-hit trend windows — metrics_daily is 1 row/day, so even 90
    # days is cheap; the real cost is latency_samples p95 sorts, hence the tight
    # default matching its 14-day retention.
    analytics_max_range_days: int = 10

    # --- Slack daily summary (mirrors clairvoyance's incoming-webhook pattern) ---
    # Off by default: an empty SLACK_WEBHOOK_URL disables the feature (no separate
    # flag, matching clairvoyance). When set, a background task posts a daily
    # cache-economics summary (hit rate, words-from-cache %, est. cost saved —
    # overall + per provider) at slack_summary_time_utc. Never affects serving.
    slack_webhook_url: str = ""
    slack_tag_users: str = (
        "<!subteam^S05KD5LN31Q>"  # comma-separated handles/groups to cc (default: Breeze Sentinels)
    )
    slack_summary_time_utc: str = "16:30"  # daily post time (16:30 UTC == 10 PM IST)
    slack_summary_tick_seconds: int = (
        300  # how often the background loop checks the clock
    )
    # Per-provider $/word for the estimated-cost-saved figure (JSON env map).
    # cost_saved(provider) = words_from_cache(provider) * rate(provider); total = sum.
    # Placeholder rates — calibrate SLACK_COST_PER_WORD to your blended provider pricing.
    slack_cost_per_word: dict = Field(
        default_factory=lambda: {
            # $/word, from dashboard billing. elevenlabs: $70.36/1.36M chars (~6 c/w).
            # gemini: $0.000293/word (confirm unit). cartesia/sarvam: placeholders.
            "cartesia": 0.000002,
            "elevenlabs": 0.00031,
            "gemini": 0.000293,
            "sarvam": 0.000005,
        }
    )
    # USD -> INR for the cost-saved DISPLAY (the Slack summary shows rupees,
    # rounded to the nearest ₹ — no paisa). Rates above stay $/word; only the
    # shown figure is converted. Adjust if the FX rate drifts.
    slack_usd_to_inr: float = 96.0

    # --- Graceful drain / clairvoyance kill switch ---
    # On shutdown (k8s preStop -> GET /drain) dragontts FIRST tells clairvoyance
    # to bypass it (so enable_tts_caching templates fall back to their upstream
    # TTS provider), THEN drains its in-flight requests, THEN exits. Restore is
    # MANUAL: an operator POSTs action=restore to clairvoyance's admin endpoint —
    # dragontts does NOT auto-restore on startup. Clairvoyance's admin endpoint is
    # HTTPBearer + require_admin, so a clairvoyance admin JWT must be supplied
    # (env-injected in prod, never baked
    # into the image). Empty CLAIRVOYANCE_URL => the notify calls are skipped
    # (no-op), so the feature degrades cleanly when unconfigured.
    clairvoyance_url: str = ""
    clairvoyance_jwt_token: str = ""
    clairvoyance_manage_path: str = "/agent/voice/breeze-buddy/admin/dragontts/manage"
    # Short timeout so the /drain preStop hook returns fast (k8s waits on it
    # before SIGTERM). Best-effort: clairvoyance's own ~60s health monitor is the
    # backstop if this call fails.
    clairvoyance_kill_switch_timeout: float = 5.0
    enable_graceful_drain: bool = True
    # Lifespan-phase BACKSTOP for in-flight drain. uvicorn's --timeout-graceful-
    # shutdown (100s in the Dockerfile) runs FIRST and drains the in-flight HTTP/
    # stream tasks the ASGI gauge tracks; by the time lifespan shutdown calls
    # wait_for_inflight_drain, inflight is usually already ~0 and this returns
    # instantly. The 120s ceiling only binds for in-flight work uvicorn doesn't
    # account for. terminationGracePeriodSeconds must therefore exceed uvicorn's
    # 100s + preStop (~8s) + lifespan cleanup (~2s) — ~110 minimum, 150 comfortable.
    graceful_drain_max_seconds: int = 120

    @property
    def configured_providers(self) -> list[str]:
        """Providers whose required credential is present at startup."""
        live: list[str] = []
        if self.cartesia_api_key:
            live.append("cartesia")
        if self.sarvam_api_key:
            live.append("sarvam")
        if self.elevenlabs_indian_residency_api_key:
            live.append("elevenlabs")
        if self.google_credentials_json or self.google_credentials_path:
            live.append("gemini")
        return live


settings = Settings()
