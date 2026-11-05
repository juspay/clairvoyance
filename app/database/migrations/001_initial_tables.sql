-- Migration: Create initial database tables
-- Description: Create outbound_number, call_execution_config, and lead_call_tracker tables

-- Create outbound_number table
CREATE TABLE IF NOT EXISTS outbound_number (
    id VARCHAR(255) PRIMARY KEY,
    number VARCHAR(20) NOT NULL UNIQUE,
    provider VARCHAR(50) CHECK (provider IN ('TWILIO', 'EXOTEL')) NOT NULL,
    status VARCHAR(50) CHECK (status IN ('AVAILABLE', 'IN_USE', 'DISABLED')) NOT NULL,
    channels INTEGER,
    maximum_channels INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outbound_numbers_status 
    ON outbound_number (status);
CREATE INDEX IF NOT EXISTS idx_outbound_numbers_provider 
    ON outbound_number (provider);

-- Create call_execution_config table
CREATE TABLE IF NOT EXISTS call_execution_config (
    id VARCHAR(255) PRIMARY KEY,
    initial_offset INTEGER NOT NULL,
    retry_offset INTEGER NOT NULL,
    call_start_time TIME NOT NULL,
    call_end_time TIME NOT NULL,
    max_retry INTEGER NOT NULL,
    calling_provider VARCHAR(50) CHECK (calling_provider IN ('TWILIO', 'EXOTEL')) NOT NULL,
    merchant_id VARCHAR(255) NOT NULL,
    workflow VARCHAR(50) CHECK (workflow IN ('order-confirmation')) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    UNIQUE(merchant_id, workflow)
);

CREATE INDEX IF NOT EXISTS idx_call_execution_config_created_at 
    ON call_execution_config (created_at);

-- Create lead_call_tracker table
CREATE TABLE IF NOT EXISTS lead_call_tracker (
    id VARCHAR(255) PRIMARY KEY,
    outbound_number_id VARCHAR(255),
    merchant_id VARCHAR(100) NOT NULL,
    workflow VARCHAR(50) CHECK (workflow IN ('order-confirmation')) NOT NULL,
    attempt_count INTEGER DEFAULT 0,
    next_attempt_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    payload JSONB,
    meta_data JSONB,
    recording_url VARCHAR(500),
    status VARCHAR(50) CHECK (status IN ('BACKLOG', 'PROCESSING', 'FINISHED', 'RETRY')) NOT NULL,
    outcome VARCHAR(50) CHECK (outcome IN ('NO_ANSWER', 'BUSY', 'CANCEL', 'CONFIRM', 'UNKNOWN', 'ADDRESS_UPDATED')),
    call_id VARCHAR(100),
    call_initiated_time TIMESTAMP WITH TIME ZONE,
    call_end_time TIMESTAMP WITH TIME ZONE,
    cost REAL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lead_call_tracker_merchant_id 
    ON lead_call_tracker (merchant_id);
CREATE INDEX IF NOT EXISTS idx_lead_call_tracker_status 
    ON lead_call_tracker (status);
CREATE INDEX IF NOT EXISTS idx_lead_call_tracker_outcome 
    ON lead_call_tracker (outcome);
CREATE INDEX IF NOT EXISTS idx_lead_call_tracker_created_at 
    ON lead_call_tracker (created_at);
