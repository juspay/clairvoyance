-- Migration 036: Durable per-day aggregates of DragonTTS cache attribution.
--
-- DragonTTS sits in front of the actual speech-synthesis provider and caches
-- previously-synthesized sentences so repeat phrases (greetings, canned
-- prompts, etc.) skip a round-trip to the underlying TTS vendor. This table
-- is the durable landing zone for that attribution data: one row per
-- (date_ist, provider, reseller_id, merchant_id, template_id) tuple, holding
-- hit/miss sentence-request counts and hit/miss word counts for the day.
--
-- ``provider`` here is the NESTED provider behind DragonTTS -- i.e. the real
-- vendor DragonTTS would have called on a cache miss (cartesia, elevenlabs,
-- sarvam, gemini, ...) -- NOT "dragontts" itself.
--
-- Rows are upserted by the ``bb_ttscache_rollup`` background task, which
-- periodically flushes the shared Redis day-counters (fast, ephemeral,
-- TTL 3 days) into this table. Because the Redis counters
-- hold full-day running totals rather than deltas, the upsert always writes
-- the ABSOLUTE current total for the day -- it overwrites, it does not
-- increment. merchant_id / template_id are nullable: some cache activity
-- (e.g. shared/global prompts) is not attributable to a single merchant or
-- template.

CREATE TABLE IF NOT EXISTS tts_cache_daily (
    id            BIGSERIAL PRIMARY KEY,
    date_ist      DATE         NOT NULL,
    provider      VARCHAR(50)  NOT NULL,
    reseller_id   VARCHAR(255) NOT NULL,
    merchant_id   VARCHAR(255),
    template_id   VARCHAR(255),
    hit_requests  INTEGER NOT NULL DEFAULT 0,
    miss_requests INTEGER NOT NULL DEFAULT 0,
    hit_words     INTEGER NOT NULL DEFAULT 0,
    miss_words    INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Expression unique index (COALESCE on the nullable dimensions) is the
-- upsert conflict target -- a plain column unique constraint can't express
-- "NULL == NULL" identity for merchant_id/template_id the way this rollup
-- needs, since two rows both missing merchant_id for the same day/provider/
-- reseller must still collide instead of accumulating duplicates.
CREATE UNIQUE INDEX IF NOT EXISTS uq_tts_cache_daily ON tts_cache_daily
    (date_ist, provider, reseller_id, COALESCE(merchant_id, ''), COALESCE(template_id, ''));

CREATE INDEX IF NOT EXISTS idx_tts_cache_daily_date
    ON tts_cache_daily (date_ist);
