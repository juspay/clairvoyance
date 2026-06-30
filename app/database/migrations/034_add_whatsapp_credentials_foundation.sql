-- Migration: Add WhatsApp credential storage foundation
-- Description:
--   Stores sensitive WhatsApp access tokens in the existing credentials table
--   and stores merchant-specific WhatsApp metadata in a dedicated table.

CREATE TABLE IF NOT EXISTS merchant_whatsapp_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reseller_id VARCHAR(255) NOT NULL,
    merchant_id VARCHAR(255) NOT NULL,
    credential_id UUID NOT NULL REFERENCES credentials(id) ON DELETE RESTRICT,
    business_id VARCHAR(255),
    waba_id VARCHAR(255) NOT NULL,
    phone_number_id VARCHAR(255) NOT NULL,
    display_phone_number VARCHAR(64),
    verified_name VARCHAR(255),
    messages_sent_count BIGINT NOT NULL DEFAULT 0,
    connected_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_message_sent_at TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    CONSTRAINT merchant_whatsapp_credentials_messages_sent_count_nonnegative
        CHECK (messages_sent_count >= 0),
    UNIQUE (reseller_id, merchant_id),
    UNIQUE (credential_id)
);

CREATE INDEX IF NOT EXISTS idx_merchant_whatsapp_credentials_reseller_merchant
    ON merchant_whatsapp_credentials(reseller_id, merchant_id);

CREATE INDEX IF NOT EXISTS idx_merchant_whatsapp_credentials_phone_number_id
    ON merchant_whatsapp_credentials(phone_number_id);
