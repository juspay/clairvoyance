-- Initialize the clairvoyance database with normalized conversation tables
-- This script runs automatically when the PostgreSQL container starts for the first time

-- Set encoding and locale
\encoding UTF8

-- Enable UUID extension for better primary keys
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Conversations table - metadata and session tracking
CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id VARCHAR(255) UNIQUE NOT NULL,
    user_id VARCHAR(255) NOT NULL,
    merchant_id VARCHAR(255) NOT NULL,
    title VARCHAR(255),
    status VARCHAR(50) DEFAULT 'active' CHECK (status IN ('active', 'completed', 'cancelled')),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_activity_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    metadata JSONB DEFAULT '{}'
);

-- Messages table - individual conversation messages
CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    conversation_id UUID NOT NULL,
    turn_number INTEGER NOT NULL,
    role VARCHAR(20) NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    metadata JSONB DEFAULT '{}',
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

-- Tool calls table - function calls and results
CREATE TABLE IF NOT EXISTS tool_calls (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    message_id UUID NOT NULL,
    tool_call_id VARCHAR(255) NOT NULL,
    function_name VARCHAR(255) NOT NULL,
    arguments JSONB NOT NULL DEFAULT '{}',
    result TEXT,
    success BOOLEAN DEFAULT NULL,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
);

-- Performance and security indexes
-- User access patterns for security
CREATE INDEX IF NOT EXISTS idx_conversations_user_merchant ON conversations(user_id, merchant_id);
CREATE INDEX IF NOT EXISTS idx_conversations_session_user ON conversations(session_id, user_id);

-- Message retrieval patterns
CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_turn ON messages(conversation_id, turn_number);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);

-- Tool call patterns
CREATE INDEX IF NOT EXISTS idx_tool_calls_message_id ON tool_calls(message_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_function ON tool_calls(function_name);

-- Cleanup and analytics patterns
CREATE INDEX IF NOT EXISTS idx_conversations_last_activity ON conversations(last_activity_at);
CREATE INDEX IF NOT EXISTS idx_conversations_updated_at ON conversations(updated_at);
CREATE INDEX IF NOT EXISTS idx_conversations_created_at ON conversations(created_at);

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