"""
Database accessor functions for normalized conversation persistence.
"""
import json
import uuid
from typing import Optional, Dict, Any, List
from app.core.logger import logger
from app.database import get_db_connection
from app.database.queries.conversation import (
    CREATE_CONVERSATION,
    GET_CONVERSATION_BY_SESSION,
    UPDATE_CONVERSATION_ACTIVITY,
    DELETE_CONVERSATION,
    INSERT_MESSAGE,
    GET_MESSAGES_BY_CONVERSATION,
    GET_LATEST_TURN_NUMBER,
    INSERT_TOOL_CALL,
    UPDATE_TOOL_CALL_RESULT,
    GET_TOOL_CALLS_BY_MESSAGE,
    GET_FULL_CONVERSATION,
    GET_CONVERSATION_WITH_TOOLS,
    CLEANUP_OLD_CONVERSATIONS,
    GET_CONVERSATION_STATS
)


# ============================================================================
# CONVERSATION MANAGEMENT
# ============================================================================

async def create_conversation(
    session_id: str, 
    user_id: str, 
    merchant_id: str, 
    title: str = None,
    metadata: Dict[str, Any] = None
) -> Optional[str]:
    """Create a new conversation and return its UUID."""
    try:
        metadata_json = json.dumps(metadata or {})
        
        async for conn in get_db_connection():
            row = await conn.fetchrow(
                CREATE_CONVERSATION,
                session_id,
                user_id,
                merchant_id,
                title,
                metadata_json
            )
            
        conversation_uuid = str(row['id'])
        logger.info(f"Created conversation {conversation_uuid} for session {session_id} (user: {user_id}, merchant: {merchant_id})")
        return conversation_uuid
        
    except Exception as e:
        logger.error(f"Failed to create conversation for session {session_id}: {e}")
        return None


async def get_conversation_by_session(session_id: str, user_id: str, merchant_id: str) -> Optional[Dict[str, Any]]:
    """Get conversation metadata by session ID with user authorization."""
    try:
        async for conn in get_db_connection():
            row = await conn.fetchrow(GET_CONVERSATION_BY_SESSION, session_id, user_id, merchant_id)
            
            if row:
                return {
                    'id': str(row['id']),
                    'session_id': row['session_id'],
                    'user_id': row['user_id'],
                    'merchant_id': row['merchant_id'],
                    'title': row['title'],
                    'status': row['status'],
                    'created_at': row['created_at'],
                    'updated_at': row['updated_at'],
                    'last_activity_at': row['last_activity_at'],
                    'metadata': json.loads(row['metadata']) if row['metadata'] else {}
                }
            return None
                
    except Exception as e:
        logger.error(f"Failed to get conversation for session {session_id}: {e}")
        return None


async def update_conversation_activity(conversation_id: str) -> bool:
    """Update last activity timestamp for a conversation."""
    try:
        async for conn in get_db_connection():
            await conn.execute(UPDATE_CONVERSATION_ACTIVITY, conversation_id)
        return True
        
    except Exception as e:
        logger.error(f"Failed to update activity for conversation {conversation_id}: {e}")
        return False


async def delete_conversation(session_id: str, user_id: str, merchant_id: str) -> bool:
    """Delete conversation and all associated messages/tool calls."""
    try:
        async for conn in get_db_connection():
            result = await conn.execute(DELETE_CONVERSATION, session_id, user_id, merchant_id)
            
        # Check if any rows were affected
        rows_affected = int(result.split()[-1]) if result else 0
        
        if rows_affected > 0:
            logger.info(f"Deleted conversation for session {session_id} (user: {user_id}, merchant: {merchant_id})")
            return True
        else:
            logger.warning(f"No authorized conversation found to delete for session {session_id}")
            return False
            
    except Exception as e:
        logger.error(f"Failed to delete conversation for session {session_id}: {e}")
        return False


# ============================================================================
# MESSAGE MANAGEMENT  
# ============================================================================

async def save_message(
    conversation_id: str,
    turn_number: int,
    role: str,
    content: str,
    metadata: Dict[str, Any] = None
) -> Optional[str]:
    """Save a message and return its UUID."""
    try:
        metadata_json = json.dumps(metadata or {})
        
        async for conn in get_db_connection():
            row = await conn.fetchrow(
                INSERT_MESSAGE,
                conversation_id,
                turn_number,
                role,
                content,
                metadata_json
            )
            
        message_uuid = str(row['id'])
        logger.debug(f"Saved {role} message for conversation {conversation_id}, turn {turn_number}")
        return message_uuid
        
    except Exception as e:
        logger.error(f"Failed to save message for conversation {conversation_id}: {e}")
        return None


async def get_messages_by_conversation(conversation_id: str) -> List[Dict[str, Any]]:
    """Get all messages for a conversation, ordered by turn and timestamp."""
    try:
        async for conn in get_db_connection():
            rows = await conn.fetch(GET_MESSAGES_BY_CONVERSATION, conversation_id)
            
        messages = []
        for row in rows:
            messages.append({
                'id': str(row['id']),
                'turn_number': row['turn_number'],
                'role': row['role'],
                'content': row['content'],
                'timestamp': row['timestamp'],
                'metadata': json.loads(row['metadata']) if row['metadata'] else {}
            })
            
        logger.debug(f"Retrieved {len(messages)} messages for conversation {conversation_id}")
        return messages
        
    except Exception as e:
        logger.error(f"Failed to get messages for conversation {conversation_id}: {e}")
        return []


async def get_latest_turn_number(conversation_id: str) -> int:
    """Get the latest turn number for a conversation."""
    try:
        async for conn in get_db_connection():
            row = await conn.fetchrow(GET_LATEST_TURN_NUMBER, conversation_id)
            
        return row['max_turn'] if row else 0
        
    except Exception as e:
        logger.error(f"Failed to get latest turn number for conversation {conversation_id}: {e}")
        return 0


# ============================================================================
# TOOL CALL MANAGEMENT
# ============================================================================

async def save_tool_call(
    message_id: str,
    tool_call_id: str,
    function_name: str,
    arguments: Dict[str, Any]
) -> Optional[str]:
    """Save a tool call and return its UUID."""
    try:
        arguments_json = json.dumps(arguments)
        
        async for conn in get_db_connection():
            row = await conn.fetchrow(
                INSERT_TOOL_CALL,
                message_id,
                tool_call_id,
                function_name,
                arguments_json
            )
            
        tool_call_uuid = str(row['id'])
        logger.debug(f"Saved tool call {function_name} for message {message_id}")
        return tool_call_uuid
        
    except Exception as e:
        logger.error(f"Failed to save tool call for message {message_id}: {e}")
        return None


async def update_tool_call_result(
    message_id: str,
    tool_call_id: str,
    result: str,
    success: bool
) -> bool:
    """Update a tool call with its result."""
    try:
        async for conn in get_db_connection():
            await conn.execute(
                UPDATE_TOOL_CALL_RESULT,
                message_id,
                tool_call_id,
                result,
                success
            )
            
        logger.debug(f"Updated tool call result for {tool_call_id}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to update tool call result for {tool_call_id}: {e}")
        return False


async def get_tool_calls_by_message(message_id: str) -> List[Dict[str, Any]]:
    """Get all tool calls for a message."""
    try:
        async for conn in get_db_connection():
            rows = await conn.fetch(GET_TOOL_CALLS_BY_MESSAGE, message_id)
            
        tool_calls = []
        for row in rows:
            tool_calls.append({
                'id': str(row['id']),
                'tool_call_id': row['tool_call_id'],
                'function_name': row['function_name'],
                'arguments': json.loads(row['arguments']),
                'result': row['result'],
                'success': row['success'],
                'timestamp': row['timestamp']
            })
            
        return tool_calls
        
    except Exception as e:
        logger.error(f"Failed to get tool calls for message {message_id}: {e}")
        return []


# ============================================================================
# COMPLETE CONVERSATION LOADING
# ============================================================================

async def load_full_conversation(session_id: str, user_id: str, merchant_id: str) -> Optional[Dict[str, Any]]:
    """Load complete conversation with all messages and tool calls."""
    try:
        async for conn in get_db_connection():
            rows = await conn.fetch(GET_CONVERSATION_WITH_TOOLS, session_id, user_id, merchant_id)
            
        if not rows:
            logger.debug(f"No conversation found for session {session_id}")
            return None
            
        # Reconstruct conversation structure
        conversation_data = None
        messages_by_id = {}
        
        for row in rows:
            # Initialize conversation metadata from first row
            if conversation_data is None:
                conversation_data = {
                    'id': str(row['conversation_id']),
                    'session_id': row['session_id'],
                    'messages': {},
                    'turns': []
                }
            
            # Process message if it exists
            if row['message_id']:
                message_id = str(row['message_id'])
                
                if message_id not in messages_by_id:
                    messages_by_id[message_id] = {
                        'id': message_id,
                        'turn_number': row['turn_number'],
                        'role': row['role'],
                        'content': row['content'],
                        'timestamp': row['message_timestamp'],
                        'tool_calls': []
                    }
                
                # Add tool call if it exists
                if row['tool_call_id']:
                    tool_call = {
                        'tool_call_id': row['tool_call_id'],
                        'function_name': row['function_name'],
                        'arguments': json.loads(row['arguments']) if row['arguments'] else {},
                        'result': row['result'],
                        'success': row['success']
                    }
                    messages_by_id[message_id]['tool_calls'].append(tool_call)
        
        # Organize messages by turn
        turns_by_number = {}
        for message in messages_by_id.values():
            turn_num = message['turn_number']
            if turn_num not in turns_by_number:
                turns_by_number[turn_num] = []
            turns_by_number[turn_num].append(message)
        
        # Create ordered turns list
        conversation_data['turns'] = []
        for turn_num in sorted(turns_by_number.keys()):
            turn_messages = sorted(turns_by_number[turn_num], key=lambda x: x['timestamp'])
            conversation_data['turns'].append({
                'turn_number': turn_num,
                'messages': turn_messages
            })
        
        logger.debug(f"Loaded full conversation for session {session_id} with {len(conversation_data['turns'])} turns")
        return conversation_data
        
    except Exception as e:
        logger.error(f"Failed to load full conversation for session {session_id}: {e}")
        return None


# ============================================================================
# CLEANUP AND STATS
# ============================================================================

async def cleanup_old_conversations(hours: int = 24) -> int:
    """Clean up conversations older than specified hours."""
    try:
        query = CLEANUP_OLD_CONVERSATIONS % hours
        
        async for conn in get_db_connection():
            result = await conn.execute(query)
            
        # Extract number of deleted rows
        rows_deleted = int(result.split()[-1]) if result else 0
        
        if rows_deleted > 0:
            logger.info(f"Cleaned up {rows_deleted} old conversations (older than {hours} hours)")
        
        return rows_deleted
        
    except Exception as e:
        logger.error(f"Failed to cleanup old conversations: {e}")
        return 0


async def get_conversation_stats() -> Dict[str, Any]:
    """Get statistics about stored conversations."""
    try:
        async for conn in get_db_connection():
            row = await conn.fetchrow(GET_CONVERSATION_STATS)
            
            if row:
                return {
                    "total_conversations": row['total_conversations'],
                    "active_last_hour": row['active_last_hour'],
                    "oldest_conversation": row['oldest_conversation'].isoformat() if row['oldest_conversation'] else None,
                    "most_recent_activity": row['most_recent_activity'].isoformat() if row['most_recent_activity'] else None
                }
            else:
                return {
                    "total_conversations": 0,
                    "active_last_hour": 0,
                    "oldest_conversation": None,
                    "most_recent_activity": None
                }
                
    except Exception as e:
        logger.error(f"Failed to get conversation stats: {e}")
        return {
            "total_conversations": 0,
            "active_last_hour": 0,
            "oldest_conversation": None,
            "most_recent_activity": None,
            "error": str(e)
        }


