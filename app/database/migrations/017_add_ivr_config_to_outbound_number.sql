-- Migration: Add ivr_config JSONB column to outbound_number
-- Purpose: Allow IVR TTS provider configuration (voice name, voice ID,
-- provider-specific settings) to be set at the outbound number level
-- instead of only inheriting from the first template's configuration.
--
-- The ivr_config column stores:
-- {
--   "tts_voice_name": "sara" | "rhea" | "mira",
--   "cartesia_voice_configurations": { "voice_id": "...", "speed": 1.0, ... },
--   "elevenlabs_voice_configurations": { "voice_id": "...", "model_id": "...", ... }
-- }

ALTER TABLE "outbound_number"
ADD COLUMN "ivr_config" JSONB DEFAULT NULL;

COMMENT ON COLUMN "outbound_number"."ivr_config" IS 'IVR-level TTS provider configuration (voice name, provider-specific voice settings). Overrides template-level voice settings for IVR menu audio when present.';
