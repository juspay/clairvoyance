"""
Database query functions for the application.
"""
from typing import Any, List, Optional
import asyncpg
from app.core.logger import logger
from app.database import get_db_connection

# Helper function to execute parameterized queries
async def run_parameterized_query(query_text: str, values: List[Any]) -> Optional[List[asyncpg.Record]]:
    """
    Execute a parameterized query and return the results.
    """
    try:
        async for conn in get_db_connection():
            if query_text.strip().upper().startswith('SELECT'):
                result = await conn.fetch(query_text, *values)
                return result
            else:
                result = await conn.fetchrow(query_text, *values)
                return [result] if result else None
    except Exception as e:
        logger.error(f"Database query error: {e}")
        return None
