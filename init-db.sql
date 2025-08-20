-- Initialize the clairvoyance database with required tables
-- This script runs automatically when the PostgreSQL container starts for the first time

-- Set encoding and locale
\encoding UTF8

-- Create conversations table for session persistence
CREATE TABLE IF NOT EXISTS conversations (
    session_id VARCHAR(255) PRIMARY KEY,
    conversation_id VARCHAR(255) NOT NULL,
    conversation_data JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_activity_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Create index on updated_at for cleanup queries
CREATE INDEX IF NOT EXISTS idx_conversations_updated_at ON conversations(updated_at);

-- Create index on created_at for analytics
CREATE INDEX IF NOT EXISTS idx_conversations_created_at ON conversations(created_at);

-- Create index on last_activity_at for cleanup queries
CREATE INDEX IF NOT EXISTS idx_conversations_last_activity_at ON conversations(last_activity_at);

-- Create function to automatically update updated_at column
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Create trigger to automatically update updated_at
DROP TRIGGER IF EXISTS update_conversations_updated_at ON conversations;
CREATE TRIGGER update_conversations_updated_at
    BEFORE UPDATE ON conversations
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Create calls table for telephony features (if needed)
CREATE TABLE IF NOT EXISTS calls (
    id VARCHAR(255) PRIMARY KEY,
    outcome TEXT,
    transcription TEXT,
    call_start_time TIMESTAMP WITH TIME ZONE,
    call_end_time TIMESTAMP WITH TIME ZONE,
    call_id VARCHAR(255),
    provider VARCHAR(100),
    status VARCHAR(50),
    requested_by VARCHAR(100),
    call_payload JSONB,
    assigned_number VARCHAR(20),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Create indexes for calls table
CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status);
CREATE INDEX IF NOT EXISTS idx_calls_provider ON calls(provider);
CREATE INDEX IF NOT EXISTS idx_calls_created_at ON calls(created_at);

-- Create trigger for calls table
DROP TRIGGER IF EXISTS update_calls_updated_at ON calls;
CREATE TRIGGER update_calls_updated_at
    BEFORE UPDATE ON calls
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Grant permissions to the application user
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO clairvoyance_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO clairvoyance_user;
GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public TO clairvoyance_user;

-- Print completion message
\echo 'Database initialization completed successfully!'