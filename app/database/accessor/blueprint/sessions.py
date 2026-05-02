"""
Database accessor functions for blueprint sessions.
"""

from typing import List, Optional

from app.core.logger import logger
from app.database.decoder.blueprint.sessions import decode_session
from app.database.queries import run_parameterized_query
from app.database.queries.blueprint.sessions import (
    create_session_query,
    delete_session_query,
    get_session_by_id_query,
    get_sessions_by_user_query,
    update_session_query,
)
from app.schemas.blueprint.session import BlueprintSessionModel


async def create_session(
    session_id: str,
    user_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    mode: str,
    template_id: Optional[str],
    langgraph_thread_id: str,
    current_step: Optional[str],
    status: str,
    created_at,
    updated_at,
    expires_at,
) -> Optional[BlueprintSessionModel]:
    """Create a new blueprint session."""
    logger.info(f"Creating blueprint session with ID: {session_id}")

    try:
        query, values = create_session_query(
            session_id,
            user_id,
            reseller_id,
            merchant_id,
            mode,
            template_id,
            langgraph_thread_id,
            current_step,
            status,
            created_at,
            updated_at,
            expires_at,
        )

        result = await run_parameterized_query(query, values)

        if result and len(result) > 0:
            decoded_result = decode_session(result[0])
            if decoded_result:
                logger.info(
                    f"Blueprint session created successfully: {decoded_result.id}"
                )
            else:
                logger.error("Blueprint session decoding failed after creation")
            return decoded_result

        logger.error("Failed to create blueprint session")
        return None

    except Exception as e:
        logger.error(f"Error creating blueprint session: {e}")
        return None


async def get_session_by_id(
    session_id: str,
) -> Optional[BlueprintSessionModel]:
    """Get a blueprint session by ID."""
    logger.info(f"Getting blueprint session by ID: {session_id}")

    try:
        query, values = get_session_by_id_query(session_id)
        result = await run_parameterized_query(query, values)

        if result and len(result) > 0:
            decoded_result = decode_session(result[0])
            if decoded_result:
                logger.info(f"Blueprint session found: {decoded_result.id}")
            else:
                logger.info(f"Blueprint session decoding failed for ID: {session_id}")
            return decoded_result

        logger.info(f"No blueprint session found with ID: {session_id}")
        return None

    except Exception as e:
        logger.error(f"Error getting blueprint session by ID: {e}")
        return None


async def get_sessions_by_user(
    user_id: str, status: Optional[str] = None
) -> List[BlueprintSessionModel]:
    """Get blueprint sessions by user ID, optionally filtered by status."""
    logger.info(f"Getting blueprint sessions for user: {user_id}, status: {status}")

    try:
        query, values = get_sessions_by_user_query(user_id, status)
        result = await run_parameterized_query(query, values)

        if not result:
            logger.info(f"No blueprint sessions found for user: {user_id}")
            return []

        sessions = []
        for row in result:
            decoded = decode_session(row)
            if decoded:
                sessions.append(decoded)

        logger.info(f"Found {len(sessions)} blueprint sessions for user: {user_id}")
        return sessions

    except Exception as e:
        logger.error(f"Error getting blueprint sessions by user: {e}")
        return []


async def update_session(
    session_id: str,
    current_step: Optional[str],
    status: Optional[str],
    result_template_id: Optional[str],
    updated_at,
) -> Optional[BlueprintSessionModel]:
    """Update a blueprint session."""
    logger.info(f"Updating blueprint session: {session_id}")

    try:
        query, values = update_session_query(
            session_id,
            current_step,
            status,
            result_template_id,
            updated_at,
        )

        result = await run_parameterized_query(query, values)

        if result and len(result) > 0:
            decoded_result = decode_session(result[0])
            if decoded_result:
                logger.info(
                    f"Blueprint session updated successfully: {decoded_result.id}"
                )
            else:
                logger.error("Blueprint session decoding failed after update")
            return decoded_result

        logger.error(f"Failed to update blueprint session: {session_id}")
        return None

    except Exception as e:
        logger.error(f"Error updating blueprint session: {e}")
        return None


async def delete_session(session_id: str) -> bool:
    """Delete a blueprint session."""
    logger.info(f"Deleting blueprint session: {session_id}")

    try:
        query, values = delete_session_query(session_id)
        result = await run_parameterized_query(query, values)

        if result and len(result) > 0:
            logger.info(f"Blueprint session deleted successfully: {session_id}")
            return True

        logger.info(f"Blueprint session not found for deletion: {session_id}")
        return False

    except Exception as e:
        logger.error(f"Error deleting blueprint session: {e}")
        return False
