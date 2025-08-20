"""
Database queries for conversation persistence.
"""

CREATE_CONVERSATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS conversations (
    session_id VARCHAR(255) PRIMARY KEY,
    conversation_id VARCHAR(255) NOT NULL,
    conversation_data JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_activity_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_conversations_last_activity 
ON conversations(last_activity_at);

CREATE INDEX IF NOT EXISTS idx_conversations_session_id 
ON conversations(session_id);
"""

INSERT_OR_UPDATE_CONVERSATION = """
INSERT INTO conversations (session_id, conversation_id, conversation_data, updated_at, last_activity_at)
VALUES ($1, $2, $3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON CONFLICT (session_id) 
DO UPDATE SET 
    conversation_data = EXCLUDED.conversation_data,
    updated_at = CURRENT_TIMESTAMP,
    last_activity_at = CURRENT_TIMESTAMP;
"""

GET_CONVERSATION_BY_SESSION = """
SELECT session_id, conversation_id, conversation_data, created_at, updated_at, last_activity_at
FROM conversations 
WHERE session_id = $1;
"""

DELETE_CONVERSATION = """
DELETE FROM conversations WHERE session_id = $1;
"""

CLEANUP_OLD_CONVERSATIONS = """
DELETE FROM conversations 
WHERE last_activity_at < NOW() - INTERVAL '%s hours';
"""

GET_CONVERSATION_STATS = """
SELECT 
    COUNT(*) as total_conversations,
    COUNT(CASE WHEN last_activity_at > NOW() - INTERVAL '1 hour' THEN 1 END) as active_last_hour,
    MIN(created_at) as oldest_conversation,
    MAX(last_activity_at) as most_recent_activity
FROM conversations;
"""