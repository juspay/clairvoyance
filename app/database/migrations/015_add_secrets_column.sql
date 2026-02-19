-- Migration: Add secrets column to template table
-- This column stores merchant-level secrets (API keys, tokens, base URLs) that should
-- be available for HTTP functions without being passed in every lead.payload call.
-- These values are merged into template_vars at runtime.

ALTER TABLE template ADD COLUMN IF NOT EXISTS secrets JSONB DEFAULT '{}';

COMMENT ON COLUMN template.secrets IS 'JSONB column storing merchant-level secrets (API keys, tokens, base URLs) that are merged into template_vars at runtime for HTTP function placeholder resolution';
