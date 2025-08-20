"""
Database accessor functions for conversation persistence.
"""
import json
from typing import Optional, Dict, Any
from app.core.logger import logger
from app.database import get_db_connection
from app.database.queries.conversation import (
    CREATE_CONVERSATIONS_TABLE,
    INSERT_OR_UPDATE_CONVERSATION,
    GET_CONVERSATION_BY_SESSION,
    DELETE_CONVERSATION,
    CLEANUP_OLD_CONVERSATIONS,
    GET_CONVERSATION_STATS
)


async def init_conversation_tables():
    """Initialize conversation tables in the database."""
    try:
        async for conn in get_db_connection():
            await conn.execute(CREATE_CONVERSATIONS_TABLE)
        logger.info("Conversation tables initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize conversation tables: {e}")
        raise


async def save_conversation(session_id: str, conversation_id: str, conversation_data: Dict[str, Any]) -> bool:
    """Save or update conversation data in the database."""
    try:
        # Convert conversation data to JSON string
        conversation_json = json.dumps(conversation_data)
        
        async for conn in get_db_connection():
            await conn.execute(
                INSERT_OR_UPDATE_CONVERSATION,
                session_id,
                conversation_id,
                conversation_json
            )
        
        logger.debug(f"Saved conversation for session {session_id}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to save conversation for session {session_id}: {e}")
        return False


async def load_conversation(session_id: str) -> Optional[Dict[str, Any]]:
    """Load conversation data from the database."""
    try:
        async for conn in get_db_connection():
            row = await conn.fetchrow(GET_CONVERSATION_BY_SESSION, session_id)
            
            if row:
                # Parse JSON data back to dict
                conversation_data = json.loads(row['conversation_data'])
                logger.debug(f"Loaded conversation for session {session_id}")
                return conversation_data
            else:
                logger.debug(f"No conversation found for session {session_id}")
                return None
                
    except Exception as e:
        logger.error(f"Failed to load conversation for session {session_id}: {e}")
        return None


async def delete_conversation(session_id: str) -> bool:
    """Delete conversation data from the database."""
    try:
        async for conn in get_db_connection():
            result = await conn.execute(DELETE_CONVERSATION, session_id)
            
        # Check if any rows were affected
        rows_affected = int(result.split()[-1]) if result else 0
        
        if rows_affected > 0:
            logger.info(f"Deleted conversation for session {session_id}")
            return True
        else:
            logger.warning(f"No conversation found to delete for session {session_id}")
            return False
            
    except Exception as e:
        logger.error(f"Failed to delete conversation for session {session_id}: {e}")
        return False


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