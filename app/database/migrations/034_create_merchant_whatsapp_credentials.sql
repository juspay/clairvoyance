-- Migration 034: Create merchant WhatsApp onboarding storage
-- Stores Meta Embedded Signup assets per merchant while keeping tokens in credentials.

BEGIN;

ALTER TABLE credentials
ADD COLUMN IF NOT EXISTS merchant_id VARCHAR(255);

CREATE INDEX IF NOT EXISTS idx_credentials_merchant_id
    ON credentials(merchant_id);

CREATE INDEX IF NOT EXISTS idx_credentials_reseller_merchant_name
    ON credentials(reseller_id, merchant_id, name);

CREATE TABLE IF NOT EXISTS merchant_whatsapp_credentials (
    id VARCHAR(255) PRIMARY KEY,
    reseller_id VARCHAR(255),
    merchant_id VARCHAR(255) NOT NULL REFERENCES merchants(merchant_id) ON DELETE CASCADE,
    business_token_credential_id UUID NOT NULL REFERENCES credentials(id),
    meta_business_id VARCHAR(255),
    waba_id VARCHAR(255) NOT NULL,
    phone_number_id VARCHAR(255) NOT NULL,
    display_phone_number VARCHAR(64),
    verified_name VARCHAR(255),
    token_type VARCHAR(64),
    token_expires_at TIMESTAMP WITH TIME ZONE,
    scope TEXT,
    app_id VARCHAR(255),
    config_id VARCHAR(255),
    graph_api_version VARCHAR(32) NOT NULL,
    status VARCHAR(50) CHECK (
        status IN ('CONNECTED', 'ERROR', 'DISCONNECTED')
    ) NOT NULL,
    webhook_subscribed BOOLEAN DEFAULT false NOT NULL,
    phone_registered BOOLEAN DEFAULT false NOT NULL,
    last_error_code VARCHAR(255),
    last_error_message TEXT,
    last_onboarding_event VARCHAR(64),
    raw_signup_payload JSONB DEFAULT '{}'::jsonb NOT NULL,
    payment_link_template_id VARCHAR(255),
    payment_link_template_name VARCHAR(255),
    payment_link_template_language VARCHAR(32),
    payment_link_template_category VARCHAR(64),
    payment_link_template_status VARCHAR(64),
    payment_link_template_created_at TIMESTAMP WITH TIME ZONE,
    payment_link_template_approved_at TIMESTAMP WITH TIME ZONE,
    last_template_error TEXT,
    messages_attempted_count BIGINT DEFAULT 0 NOT NULL,
    messages_success_count BIGINT DEFAULT 0 NOT NULL,
    messages_failed_count BIGINT DEFAULT 0 NOT NULL,
    last_message_attempted_at TIMESTAMP WITH TIME ZONE,
    last_message_success_at TIMESTAMP WITH TIME ZONE,
    last_message_failed_at TIMESTAMP WITH TIME ZONE,
    connected_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_merchant_whatsapp_credentials_merchant_id
    ON merchant_whatsapp_credentials(merchant_id);

CREATE INDEX IF NOT EXISTS idx_merchant_whatsapp_credentials_reseller_id
    ON merchant_whatsapp_credentials(reseller_id);

CREATE INDEX IF NOT EXISTS idx_merchant_whatsapp_credentials_waba_id
    ON merchant_whatsapp_credentials(waba_id);

CREATE INDEX IF NOT EXISTS idx_merchant_whatsapp_credentials_phone_number_id
    ON merchant_whatsapp_credentials(phone_number_id);

CREATE INDEX IF NOT EXISTS idx_merchant_whatsapp_credentials_token_credential_id
    ON merchant_whatsapp_credentials(business_token_credential_id);

CREATE INDEX IF NOT EXISTS idx_merchant_whatsapp_credentials_template_status
    ON merchant_whatsapp_credentials(payment_link_template_status);

COMMIT;
