-- Migration: Add configurations JSONB column to template table
-- Description:
--   Add configurations JSONB column to support flexible configuration storage
--   This stores TTS voice name, STT language, and other template-specific configurations

-- Add configurations column as JSONB
ALTER TABLE template
ADD COLUMN IF NOT EXISTS configurations JSONB;

-- Add comment to document the column
COMMENT ON COLUMN template.configurations IS 'JSONB object containing template configurations such as tts_voice_name (e.g., "rhea", "sara") and stt_language (e.g., "en", "hi"). This allows flexible configuration storage.';
