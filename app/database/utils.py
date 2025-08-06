"""
Database utility functions for common operations.
"""
from typing import Any, Dict, List, Optional, Type, TypeVar
from uuid import UUID
import asyncpg
from app.core.logger import logger

T = TypeVar('T')

async def get_by_id(conn: asyncpg.Connection, table_name: str, id: UUID) -> Optional[Dict[str, Any]]:
    """
    Get a record by its ID.
    """
    try:
        query = f"SELECT * FROM {table_name} WHERE id = $1"
        row = await conn.fetchrow(query, id)
        return dict(row) if row else None
    except asyncpg.PostgresError as e:
        logger.error(f"Error getting from {table_name} by ID: {e}")
        return None

async def create_record(conn: asyncpg.Connection, table_name: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Create a new record.
    """
    try:
        columns = ", ".join(data.keys())
        placeholders = ", ".join([f"${i+1}" for i in range(len(data))])
        query = f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders}) RETURNING *"
        row = await conn.fetchrow(query, *data.values())
        return dict(row) if row else None
    except asyncpg.PostgresError as e:
        logger.error(f"Error creating in {table_name}: {e}")
        return None

async def update_record(conn: asyncpg.Connection, table_name: str, id: UUID, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Update an existing record.
    """
    try:
        set_clause = ", ".join([f"{key} = ${i+2}" for i, key in enumerate(data.keys())])
        query = f"UPDATE {table_name} SET {set_clause} WHERE id = $1 RETURNING *"
        row = await conn.fetchrow(query, id, *data.values())
        return dict(row) if row else None
    except asyncpg.PostgresError as e:
        logger.error(f"Error updating {table_name}: {e}")
        return None

async def delete_record(conn: asyncpg.Connection, table_name: str, id: UUID) -> bool:
    """
    Delete a record.
    """
    try:
        query = f"DELETE FROM {table_name} WHERE id = $1"
        result = await conn.execute(query, id)
        return result == "DELETE 1"
    except asyncpg.PostgresError as e:
        logger.error(f"Error deleting from {table_name}: {e}")
        return False

async def get_all(conn: asyncpg.Connection, table_name: str, skip: int = 0, limit: int = 100) -> List[Dict[str, Any]]:
    """
    Get all records of a model with pagination.
    """
    try:
        query = f"SELECT * FROM {table_name} OFFSET $1 LIMIT $2"
        rows = await conn.fetch(query, skip, limit)
        return [dict(row) for row in rows]
    except asyncpg.PostgresError as e:
        logger.error(f"Error getting all from {table_name}: {e}")
        return []
