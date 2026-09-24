-- Migration: credentials name the provider account they hold
-- Description: a credential row was a placeholder value ({name} in a
-- template, an HTTP auth header, a CRM connector's token). It may now also
-- be a PROVIDER ACCOUNT — the key an LLM / STT / TTS service is built with —
-- so one template document can be copied and pointed at a different account
-- by changing one id. `provider` says which service the value is for
-- (azure_openai, openai, google_vertex, deepgram, soniox, sarvam, elevenlabs,
-- cartesia, google, gemini, openai_realtime, xai_realtime,
-- azure_openai_realtime); NULL keeps every existing row exactly as it was.
-- Vocabulary lives in code (app/ai/voice/agents/breeze_buddy/accounts/types.py),
-- never in a CHECK: a new provider is a code change, not a migration.
ALTER TABLE credentials
    ADD COLUMN IF NOT EXISTS provider varchar NULL;

-- The console's picker: "every active account for this provider a template
-- of this tenant may use" — reseller-wide and merchant rows in one scan.
CREATE INDEX IF NOT EXISTS credentials_provider_ix
    ON credentials (provider, reseller_id, merchant_id)
    WHERE is_active AND provider IS NOT NULL;
