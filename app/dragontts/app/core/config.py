"""Application configuration — env-backed settings + provider defaults.

Provider API-key env names mirror clairvoyance so the same k8s Secret can be
reused across both services.
"""

from __future__ import annotations

from pydantic import Field, field_validator
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
        "speed": 0.9,
        "pitch": 0.0,
    },
    "elevenlabs": {
        "voice_id": "fG9s0SXJb213f4UxVHyG",
        "model": "eleven_flash_v2_5",
        "speed": 1.15,
        "language": "en",
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
    # --- Length-scaled TTL (applied to EVERY stored entry: write-through AND
    # warmer). ttl_for(words) = clamp(BASE + PER_WORD*words, BASE, MAX), so
    # longer phrases survive longer (more synth savings to preserve) and short
    # ones age out faster. There is no permanent tier: the periodic purge job
    # (TTL_PURGE_INTERVAL_SECONDS) deletes expired rows + blobs. Set BASE<=0 to
    # disable TTL entirely (entries never expire, no purge). Supersedes the flat
    # ttl_seconds knob below, which is kept only for back-compat.
    cache_ttl_base_seconds: int = 172800  # 48h floor — min TTL for any phrase
    cache_ttl_per_word_seconds: int = 21600  # +6h per word
    cache_ttl_max_seconds: int = 518400  # 6d cap
    ttl_purge_interval_seconds: int = 1200  # purge sweep cadence (20 min)
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
    # (no space) if missing, leave it if already present. Other providers read a
    # leading "." fine, so they're unaffected.
    tts_leading_dot: bool = True
    # Split an incoming transcript into sentences BEFORE synth/serve, so each
    # sentence is synthesized + cached under its own key (reusable by any later
    # request that sends that sentence alone — e.g. recurring greetings). Set to
    # the symbols to split AFTER, e.g. ".:?!" — the split fires at whitespace
    # following one of these, so decimals ("3.14") and URLs are not broken. Empty
    # (default) = OFF -> the whole transcript is one entry (today's behavior).
    # The synth and stream paths are controlled INDEPENDENTLY (each can be off, or
    # use a different symbol set):
    split_at_symbols: str = ""          # /tts/bytes  (one-shot synth) path
    split_at_symbols_stream: str = ""   # /tts/stream path
    # Only split when EVERY resulting part has at least this many words. A lone
    # single-word part (e.g. "hello" in "hello. how are you") is too small to be
    # worth caching on its own and would needlessly fragment a phrase, so when any
    # part is shorter the WHOLE phrase stays one entry. Default 2 = "more than 1
    # word". Set to 1 to split even single-word sentences.
    split_min_words_per_part: int = 2
    # Split parts are pure-concatenated (no edge trim, no inserted gap) — nothing
    # is cut and each part is appended exactly as synthesized. There is no gap
    # knob: the provider's own leading/trailing silence between parts is left
    # intact. (If you later want a normalized inter-sentence gap, re-introduce it
    # in _get_or_synthesize_split / _stream_split.)

    # --- Performance ---
    thread_pool_workers: int = 32  # asyncio.to_thread pool size
    bulk_create_max: int = 1000  # hard cap on /tts/create/bulk items
    # Number of warm, persistent Cartesia streaming sockets kept ready for cache
    # misses (each multiplexes many utterances by context_id). Set via env, e.g.
    # CARTESIA_STREAM_POOL_SIZE=4. 0 => open a fresh socket per miss (no pooling).
    cartesia_stream_pool_size: int = 2
    # Warm ElevenLabs multi-context WS sockets, pooled PER VOICE (the voice is in
    # the WS URL). Each socket multiplexes up to 5 concurrent contexts. Streaming
    # misses reuse a warm socket; if none is ready, fall back to one-shot HTTP.
    elevenlabs_stream_pool_size: int = 2
    # Max server-silence gap (seconds) after audio starts that ends an ElevenLabs
    # WS utterance (ElevenLabs delays is_final ~20s). Lower = faster stream
    # close/turn-end; raise if long utterances ever truncate at a >N s pause.
    elevenlabs_stream_idle_timeout: float = 0.8
    # Warm Sarvam WS sockets. Sarvam is NOT multiplexed (one utterance per socket
    # at a time), so this is a LIFO stack of warm, pre-configured connections. 0
    # => stream via a fresh socket per miss (no pooling).
    sarvam_stream_pool_size: int = 2
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
    provider_resilience_overrides: dict = Field(default_factory=dict)
    # --- Write-behind metrics (off the hot HIT path) ---
    # HIT touch/metric updates are batched + flushed by a background task so a HIT
    # returns audio without awaiting a SQLite write. Flush by interval or batch
    # size, and on graceful shutdown. enabled=false -> synchronous writes.
    metrics_write_behind_enabled: bool = True
    metrics_flush_interval_ms: int = 500
    metrics_flush_batch_size: int = 64
    # Latency sampling: fraction of requests timed for the avg/p95 rollup (0
    # disables). perf_counter is cheap; sampling bounds latency_samples growth.
    metrics_latency_sample_rate: float = 0.3  # fraction of requests timed for the latency rollup
    # latency_samples rows older than this are pruned by the periodic checkpoint
    # loop, keeping the table bounded.
    metrics_latency_retention_days: int = 14
    # --- Predictive cache warming (Part 1: frequency-based auto-warm) ---
    # Tracks recurring phrase substrings across requests and warms the frequent
    # ones into the cache so Part 2 (segment + stitch) can assemble them.
    predictive_warm_enabled: bool = False  # off by default (stitch/warm retired; write-through only)
    # Length-scaled warm threshold. A short phrase (e.g. "hi") is a substring of
    # many longer ones, so its decayed count is the sum of all of them — it
    # would dominate warming and cache trivial fragments. Longer phrases are the
    # valuable scripted lines but occur less often. So the threshold STARTS HIGH
    # for the shortest tracked phrase and steps DOWN per extra word, to a floor:
    #   threshold(len) = max(threshold - (len - min_words) * step, floor)
    # e.g. threshold=3.0, step=0.5, floor=1.5, min_words=1 ->
    #     1 word=3.0, 2=2.5, 3=2.0, 4=1.5, 5..6=1.5 (floored).
    # Set step=0.0 for a flat threshold (legacy behaviour).
    predictive_warm_threshold: float = 3.0  # base threshold (shortest phrase)
    predictive_warm_threshold_step: float = 0.5  # subtracted per extra word
    predictive_warm_threshold_floor: float = 1.5  # never below this
    predictive_warm_min_words: int = 1  # shortest phrase length to track
    predictive_warm_max_words: int = 6  # cap on contiguous-substring length
    # Characters that end a sub-phrase segment: a token whose last char is in
    # this string closes the segment, so sub-phrases never span one. Each char is
    # a delimiter; default "." splits on sentence-ending periods. Punctuation is
    # KEPT on the token, so warmed keys match live request keys + stitch lookups.
    # Add chars (e.g. ".?!") to also split on those.
    predictive_warm_split_chars: str = "."
    predictive_warm_decay_factor: float = 0.94  # counts x this each interval; with the 5-min interval below this is a ~1h half-life
    predictive_warm_decay_interval_s: int = 300  # how often decay runs (5 min)
    predictive_warm_min_floor: float = 0.5  # prune counts below this after decay
    # Slice a warmed phrase's timestamped audio into every contiguous sub-phrase
    # (Cartesia only — needs word boundaries). OFF by default: the recurring
    # phrase is already cached whole by the request path, and the sub-phrase
    # entries exist only to feed stitch's assembly. Flip to true to opt back into
    # the substring-closed split (stitch then has a substrate to assemble from);
    # the tracker still suppresses sub-phrases either way.
    predictive_warm_split_enabled: bool = False
    # --- Predictive stitching (Part 2: serve a MISS from cached sub-phrases) ---
    # On a full-text MISS, binary-search cached prefix/suffix, synth only the
    # gaps, cross-fade at seams. Skipped below the coverage gate.
    predictive_stitch_enabled: bool = False  # off by default (retired; re-enable per-env to revive)
    # Stitch a MISS from cached sub-phrases when at least this fraction is cached
    # (miss <= 1 - this). Default 0.25 = latency-favored (stitch when miss < 75%):
    # the gap synth is <=75% of the phrase so it still beats a full synth on the
    # first request, and the assembled clip is cached for instant repeat HITs.
    # Override per-env via PREDICTIVE_STITCH_MIN_COVERAGE — raise to 0.5 for
    # quality-favored (fewer seams); don't lower below ~0.2 (assembly overhead
    # then outweighs the synth savings).
    predictive_stitch_min_coverage: float = 0.25
    # Apply stitch on the /tts/stream path too (assemble + stream + cache).
    # Independent of the one-shot flag: live streaming has lower TTFB, so this is
    # the trade of "first request waits for gap-synth + assembly" vs "reuse cached
    # sub-phrases and cache the assembled clip for instant repeat HITs".
    predictive_stitch_stream_enabled: bool = False  # off by default (stitch retired)
    # --- Progressive pass-through stitch (opt-in /tts/stream path) ---
    # When true AND /tts/stream is a stitchable MISS with a cached PREFIX (>= the
    # min-words gate below) AND the requested format == native pcm_s16le@16k,
    # stream the cached prefix IMMEDIATELY while the first gap synthesizes
    # (TTFB ~0), holding only the ~xfade window per seam. Purely additive: when
    # false (default) the existing assemble-then-stream stitch path is byte-for-
    # byte unchanged, and on any ineligibility/error this falls back to it.
    # Mid-utterance pause remains for later gaps (only live gap-streaming removes
    # it) -- this only fixes the START (perceptually-important) latency.
    enable_pass_through_stitch: bool = False
    # Minimum cached-prefix word count to use the pass-through path. Below this
    # (or no cached prefix before the first gap) it falls back to the assemble
    # path (a 1-2 word prefix isn't worth streaming early).
    pass_through_stitch_min_words: int = 2
    # --- Stitch seam-DSP knobs (numpy) — tune assembled-clip quality via env. ---
    predictive_stitch_xfade_ms: float = 8.0         # crossfade overlap at each splice. Stitch joins UNRELATED fragments, so a long window (the old 30ms) audibly doubles/smears the seam and can clip on correlated edges; ~8ms + the zero-crossing anchor is clean. Env-tunable.
    predictive_stitch_target_rms_db: float = -20.0  # per-fragment loudness target (speech ~-23..-18 dBFS)
    predictive_stitch_rms_floor_db: float = -55.0   # below this a fragment isn't amplified (don't hiss up a breath/gap)
    predictive_stitch_sil_relative_db: float = 25.0 # silence gate: a window this many dB below the clip peak is trimmed (HIGHER = more aggressive gap cutting)
    predictive_stitch_sil_guard_ms: float = 3.0     # sliver kept at each trimmed edge so onsets/offsets survive
    predictive_stitch_zc_search_ms: float = 4.0     # window scanned for a zero crossing to anchor each splice (avoids clicks)

    # --- Slack daily summary (mirrors clairvoyance's incoming-webhook pattern) ---
    # Off by default: an empty SLACK_WEBHOOK_URL disables the feature (no separate
    # flag, matching clairvoyance). When set, a background task posts a daily
    # cache-economics summary (hit rate, words-from-cache %, est. cost saved —
    # overall + per provider) at slack_summary_time_utc. Never affects serving.
    slack_webhook_url: str = ""
    slack_tag_users: str = "<!subteam^S05KD5LN31Q>"  # comma-separated handles/groups to cc (default: Breeze Sentinels)
    slack_summary_time_utc: str = "16:30"    # daily post time (16:30 UTC == 10 PM IST)
    slack_summary_tick_seconds: int = 300    # how often the background loop checks the clock
    # Per-provider $/word for the estimated-cost-saved figure (JSON env map).
    # cost_saved(provider) = words_from_cache(provider) * rate(provider); total = sum.
    # Placeholder rates — calibrate SLACK_COST_PER_WORD to your blended provider pricing.
    slack_cost_per_word: dict = Field(default_factory=lambda: {
        # $/word, from dashboard billing. elevenlabs: $70.36/1.36M chars (~6 c/w).
        # gemini: $0.000293/word (confirm unit). cartesia/sarvam: placeholders.
        "cartesia": 0.000002, "elevenlabs": 0.00031, "gemini": 0.000293, "sarvam": 0.000005,
    })
    # USD -> INR for the cost-saved DISPLAY (the Slack summary shows rupees,
    # rounded to the nearest ₹ — no paisa). Rates above stay $/word; only the
    # shown figure is converted. Adjust if the FX rate drifts.
    slack_usd_to_inr: float = 96.0

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
