"""
Database queries for normalized conversation persistence.
"""

# ============================================================================
# CONVERSATION QUERIES
# ============================================================================

CREATE_CONVERSATION = """
INSERT INTO conversations (session_id, user_id, merchant_id, title, metadata)
VALUES ($1, $2, $3, $4, $5)
RETURNING id, session_id, created_at;
"""

GET_CONVERSATION_BY_SESSION = """
SELECT id, session_id, user_id, merchant_id, title, status, created_at, updated_at, last_activity_at, metadata
FROM conversations 
WHERE session_id = $1 AND user_id = $2 AND merchant_id = $3;
"""

UPDATE_CONVERSATION_ACTIVITY = """
UPDATE conversations 
SET last_activity_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
WHERE id = $1;
"""

DELETE_CONVERSATION = """
DELETE FROM conversations 
WHERE session_id = $1 AND user_id = $2 AND merchant_id = $3;
"""

# ============================================================================
# MESSAGE QUERIES
# ============================================================================

INSERT_MESSAGE = """
INSERT INTO messages (conversation_id, turn_number, role, content, metadata)
VALUES ($1, $2, $3, $4, $5)
RETURNING id, timestamp;
"""

GET_MESSAGES_BY_CONVERSATION = """
SELECT id, turn_number, role, content, timestamp, metadata
FROM messages 
WHERE conversation_id = $1 
ORDER BY turn_number ASC, timestamp ASC;
"""

GET_LATEST_TURN_NUMBER = """
SELECT COALESCE(MAX(turn_number), 0) as max_turn
FROM messages 
WHERE conversation_id = $1;
"""

# ============================================================================
# TOOL CALL QUERIES
# ============================================================================

INSERT_TOOL_CALL = """
INSERT INTO tool_calls (message_id, tool_call_id, function_name, arguments)
VALUES ($1, $2, $3, $4)
RETURNING id, timestamp;
"""

UPDATE_TOOL_CALL_RESULT = """
UPDATE tool_calls 
SET result = $3, success = $4, timestamp = CURRENT_TIMESTAMP
WHERE message_id = $1 AND tool_call_id = $2;
"""

GET_TOOL_CALLS_BY_MESSAGE = """
SELECT id, tool_call_id, function_name, arguments, result, success, timestamp
FROM tool_calls 
WHERE message_id = $1 
ORDER BY timestamp ASC;
"""

# ============================================================================
# CONVERSATION WITH MESSAGES (JOIN QUERIES)
# ============================================================================

GET_FULL_CONVERSATION = """
SELECT 
    c.id as conversation_id,
    c.session_id,
    c.user_id,
    c.merchant_id,
    c.title,
    c.status,
    c.created_at as conversation_created_at,
    c.last_activity_at,
    m.id as message_id,
    m.turn_number,
    m.role,
    m.content,
    m.timestamp as message_timestamp,
    m.metadata as message_metadata
FROM conversations c
LEFT JOIN messages m ON c.id = m.conversation_id
WHERE c.session_id = $1 AND c.user_id = $2 AND c.merchant_id = $3
ORDER BY m.turn_number ASC, m.timestamp ASC;
"""

GET_CONVERSATION_WITH_TOOLS = """
SELECT 
    c.id as conversation_id,
    c.session_id,
    m.id as message_id,
    m.turn_number,
    m.role,
    m.content,
    m.timestamp as message_timestamp,
    tc.tool_call_id,
    tc.function_name,
    tc.arguments,
    tc.result,
    tc.success
FROM conversations c
LEFT JOIN messages m ON c.id = m.conversation_id
LEFT JOIN tool_calls tc ON m.id = tc.message_id
WHERE c.session_id = $1 AND c.user_id = $2 AND c.merchant_id = $3
ORDER BY m.turn_number ASC, m.timestamp ASC, tc.timestamp ASC;
"""

# ============================================================================
# CLEANUP AND STATS QUERIES
# ============================================================================

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

GET_MESSAGE_STATS = """
SELECT 
    COUNT(*) as total_messages,
    COUNT(CASE WHEN role = 'user' THEN 1 END) as user_messages,
    COUNT(CASE WHEN role = 'assistant' THEN 1 END) as assistant_messages,
    AVG(LENGTH(content)) as avg_message_length
FROM messages m
JOIN conversations c ON m.conversation_id = c.id
WHERE c.user_id = $1 AND c.merchant_id = $2;
"""