-- Migration: Add ivr_config JSONB column to outbound_number
-- Purpose: Allow IVR voice configuration (voice name, voice ID, greeting, goodbye)
-- to be set at the outbound number level instead of only at the template level.
--
-- The ivr_config column stores:
-- {
--   "tts_voice_name": "sara" | "rhea" | "mira",
--   "ivr_greeting": "Welcome to support. Press 1 for ...",
--   "ivr_goodbye": "Goodbye.",
--   "cartesia_voice_configurations": { "voice_id": "...", "speed": 1.0, ... },
--   "elevenlabs_voice_configurations": { "voice_id": "...", "model_id": "...", ... }
-- }

ALTER TABLE "outbound_number"
ADD COLUMN "ivr_config" JSONB DEFAULT NULL;

COMMENT ON COLUMN "outbound_number"."ivr_config" IS 'IVR-level voice configuration (voice name, greeting, goodbye, provider-specific voice settings). Overrides template-level IVR settings when present.';
