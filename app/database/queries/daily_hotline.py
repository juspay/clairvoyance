"""
Database query functions for Hotline room reservation system.
"""
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timedelta
from uuid import UUID
import asyncpg

from app.core.logger import logger
from app.schemas import DailyRoomStatus


# Table names
HOTLINE_ROOMS_TABLE = "daily_hotline_rooms"


def get_available_rooms_query(limit: int = 5) -> Tuple[str, List[Any]]:
    """
    Generate query to get multiple available rooms - sorting handled in application layer.
    """
    text = f"""
        SELECT "id", "daily_room_url", "daily_token", "agent_pid", "created_at", "expires_at"
        FROM "{HOTLINE_ROOMS_TABLE}"
        WHERE "status" = $1 AND "expires_at" > NOW() AND "isactive" = true
        LIMIT $2;
    """
    values = [DailyRoomStatus.AVAILABLE.value, limit]
    return text, values
def reserve_room_query(room_id: UUID, session_id: str = None) -> Tuple[str, List[Any]]:
    """
    Generate query to atomically reserve a room (mark as 'reserved') and optionally set session_id.
    """
    if session_id:
        text = f"""
            UPDATE "{HOTLINE_ROOMS_TABLE}"
            SET "status" = $2, "session_id" = $3
            WHERE "id" = $1 AND "status" = $4 AND "expires_at" > NOW() AND "isactive" = true;
        """
        values = [room_id, DailyRoomStatus.RESERVED.value, session_id, DailyRoomStatus.AVAILABLE.value]
    else:
        text = f"""
            UPDATE "{HOTLINE_ROOMS_TABLE}"
            SET "status" = $2
            WHERE "id" = $1 AND "status" = $3 AND "expires_at" > NOW() AND "isactive" = true;
        """
        values = [room_id, DailyRoomStatus.RESERVED.value, DailyRoomStatus.AVAILABLE.value]
    return text, values

def mark_room_in_use_query(room_id: UUID) -> Tuple[str, List[Any]]:
    """
    Generate query to mark a reserved room as in use.
    """
    text = f"""
        UPDATE "{HOTLINE_ROOMS_TABLE}"
        SET "status" = $2
        WHERE "id" = $1 AND "status" = $3 AND "isactive" = true;
    """
    values = [room_id, DailyRoomStatus.IN_USE.value, DailyRoomStatus.RESERVED.value]
    return text, values
def create_room_query(
    daily_room_url: str,
    daily_token: str,
    agent_pid: int,
    expires_at: datetime,
    session_id: str = None
) -> Tuple[str, List[Any]]:
    """
    Generate query to create a new hotline room with AVAILABLE status.
    """
    if session_id:
        text = f"""
            INSERT INTO "{HOTLINE_ROOMS_TABLE}" 
            ("daily_room_url", "daily_token", "status", "agent_pid", "expires_at", "session_id")
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING "id", "daily_room_url", "daily_token", "agent_pid", "created_at", "expires_at", "session_id";
        """
        values = [daily_room_url, daily_token, DailyRoomStatus.AVAILABLE.value, agent_pid, expires_at, session_id]
    else:
        text = f"""
            INSERT INTO "{HOTLINE_ROOMS_TABLE}" 
            ("daily_room_url", "daily_token", "status", "agent_pid", "expires_at")
            VALUES ($1, $2, $3, $4, $5)
            RETURNING "id", "daily_room_url", "daily_token", "agent_pid", "created_at", "expires_at", "session_id";
        """
        values = [daily_room_url, daily_token, DailyRoomStatus.AVAILABLE.value, agent_pid, expires_at]
    return text, values

def create_room_only_query(daily_room_url: str, daily_token: str, expires_at: datetime) -> Tuple[str, List[Any]]:
    """
    Generate query to create a room without an agent (voice-independent).
    """
    text = f"""
        INSERT INTO "{HOTLINE_ROOMS_TABLE}" 
        ("daily_room_url", "daily_token", "status", "agent_pid", "expires_at")
        VALUES ($1, $2, $3, NULL, $4)
        RETURNING "id", "daily_room_url", "daily_token", "agent_pid", "created_at", "expires_at", "session_id";
    """
    values = [daily_room_url, daily_token, DailyRoomStatus.AVAILABLE.value, expires_at]
    return text, values

def update_room_agent_query(room_id: UUID, agent_pid: int) -> Tuple[str, List[Any]]:
    """
    Generate query to update room with agent PID after agent is spawned.
    """
    text = f"""
        UPDATE "{HOTLINE_ROOMS_TABLE}"
        SET "agent_pid" = $2, "updated_at" = NOW()
        WHERE "id" = $1 AND "isactive" = true
        RETURNING "id", "daily_room_url", "daily_token", "agent_pid";
    """
    values = [room_id, agent_pid]
    return text, values

def get_pool_stats_query() -> Tuple[str, List[Any]]:
    """
    Generate query to get all pool statistics in a single optimized query.
    """
    text = f"""
        SELECT 
            COUNT(*) as "total_rooms",
            COUNT(*) FILTER (WHERE "status" = $1) as "available_rooms",
            COUNT(*) FILTER (WHERE "status" = $2) as "reserved_rooms",
            COUNT(*) FILTER (WHERE "status" = $3) as "in_use_rooms"
        FROM "{HOTLINE_ROOMS_TABLE}"
        WHERE "expires_at" > NOW() AND "isactive" = true;
    """
    values = [
        DailyRoomStatus.AVAILABLE.value,
        DailyRoomStatus.RESERVED.value, 
        DailyRoomStatus.IN_USE.value
    ]
    return text, values
def cleanup_expired_rooms_query() -> Tuple[str, List[Any]]:
    """
    Generate query to soft delete expired rooms by setting isactive = false.
    """
    text = f"""
        UPDATE "{HOTLINE_ROOMS_TABLE}"
        SET "isactive" = false
        WHERE "expires_at" <= NOW() AND "isactive" = true;
    """
    values = []
    return text, values

def soft_delete_rooms_by_ids_query(room_ids: List[UUID]) -> Tuple[str, List[Any]]:
    """
    Generate query to soft delete multiple rooms by IDs (set isactive = false).
    """
    placeholders = ', '.join([f'${i+1}' for i in range(len(room_ids))])
    text = f"""
        UPDATE "{HOTLINE_ROOMS_TABLE}"
        SET "isactive" = false
        WHERE "id" IN ({placeholders}) AND "isactive" = true;
    """
    values = room_ids
    return text, values

def delete_rooms_by_ids_query(room_ids: List[UUID]) -> Tuple[str, List[Any]]:
    """
    Generate query to hard delete multiple rooms by IDs (completely remove from database).
    Use only for cleanup of soft-deleted records or emergency situations.
    """
    placeholders = ', '.join([f'${i+1}' for i in range(len(room_ids))])
    text = f"""
        DELETE FROM "{HOTLINE_ROOMS_TABLE}"
        WHERE "id" IN ({placeholders});
    """
    values = room_ids
    return text, values

def cleanup_rooms_by_ids_query(room_ids: List[UUID]) -> Tuple[str, List[Any]]:
    """
    Generate query to cleanup multiple rooms by IDs (set status back to AVAILABLE and clear agent/session data).
    """
    placeholders = ', '.join([f'${i+2}' for i in range(len(room_ids))])
    text = f"""
        UPDATE "{HOTLINE_ROOMS_TABLE}"
        SET "status" = $1, "session_id" = NULL, "agent_pid" = NULL
        WHERE "id" IN ({placeholders}) AND "isactive" = true;
    """
    values = [DailyRoomStatus.AVAILABLE.value] + room_ids
    return text, values


def release_room_by_session_query(session_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to release a room by session_id (mark as AVAILABLE and clear session_id).
    """
    text = f"""
        UPDATE "{HOTLINE_ROOMS_TABLE}"
        SET "status" = $1, "session_id" = NULL
        WHERE "session_id" = $2 AND "status" IN ($3, $4) AND "isactive" = true;
    """
    values = [DailyRoomStatus.AVAILABLE.value, session_id, DailyRoomStatus.RESERVED.value, DailyRoomStatus.IN_USE.value]
    return text, values


def get_all_active_rooms_query() -> Tuple[str, List[Any]]:
    """
    Generate query to get all non-expired active rooms - filtering done in application layer.
    """
    text = f"""
        SELECT "id", "agent_pid", "daily_room_url", "status", "created_at", "expires_at"
        FROM "{HOTLINE_ROOMS_TABLE}"
        WHERE "expires_at" > NOW() AND "isactive" = true;
    """
    values = []
    return text, values
    values = []
    return text, values

def release_room_query(room_id: UUID) -> Tuple[str, List[Any]]:
    """
    Generate query to release a room back to available status - simplified condition.
    """
    text = f"""
        UPDATE "{HOTLINE_ROOMS_TABLE}"
        SET "status" = $2
        WHERE "id" = $1 AND "status" != $2 AND "isactive" = true;
    """
    values = [room_id, DailyRoomStatus.AVAILABLE.value]
    return text, values
