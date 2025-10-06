"""
API Router for the Automatic Voice Agent
"""

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.logger import logger

# Import global state from main. It's not ideal, but necessary for this refactor
# without a larger architectural change.
from app.helpers.automatic.daily_room_pool import get_room_pool
from app.helpers.automatic.process_pool import get_voice_agent_pool
from app.helpers.automatic.session_manager import (
    bot_procs,
    session_cleanup_callback,
)

router = APIRouter()


@router.get("/pool/status", tags=["Pools"])
async def get_pool_status():
    """Get voice agent and room pool status."""
    try:
        pool = get_voice_agent_pool()
        voice_stats = await pool.get_pool_stats()

        room_pool = get_room_pool()
        room_stats = await room_pool.get_pool_stats()

        return JSONResponse(
            {
                "status": "healthy",
                "voice_pool_stats": voice_stats,
                "room_pool_stats": room_stats,
            }
        )
    except Exception as e:
        logger.error(f"Error getting pool status: {e}")
        return JSONResponse(
            status_code=500, content={"status": "error", "message": str(e)}
        )


@router.get("/pool/rooms/status", tags=["Pools"])
async def get_room_pool_status():
    """Get Daily room pool status."""
    try:
        room_pool = get_room_pool()
        stats = await room_pool.get_pool_stats()
        return JSONResponse({"status": "healthy", "room_pool_stats": stats})
    except Exception as e:
        logger.error(f"Error getting room pool status: {e}")
        return JSONResponse(
            status_code=500, content={"status": "error", "message": str(e)}
        )


@router.post("/cleanup/{session_id}", tags=["Sessions"])
async def cleanup_session(session_id: str):
    """Cleanup a specific voice agent session."""
    try:
        pool = get_voice_agent_pool()
        pid_to_cleanup = None
        proc_info_to_cleanup = None

        # Find the process by session_id
        for pid, proc_info in list(bot_procs.items()):
            # Ensure the entry has the modern 4-tuple format before checking session_id
            if len(proc_info) >= 4 and proc_info[2] == session_id:
                pid_to_cleanup = pid
                proc_info_to_cleanup = proc_info
                break

        if pid_to_cleanup and proc_info_to_cleanup:
            proc, _, _, proc_type = proc_info_to_cleanup
            logger.info(
                f"Cleaning up session {session_id} (PID: {pid_to_cleanup}, type: {proc_type})"
            )

            # Clean up room first
            room_pool = get_room_pool()
            await room_pool.cleanup_and_replenish_room(session_id)

            # Handle process cleanup
            if proc_type == "pool":
                await pool.return_process(session_id)
                logger.info(f"Returned process to pool for session {session_id}")
            else:
                # Terminate direct process
                try:
                    if hasattr(proc, "poll") and proc.poll() is None:
                        proc.terminate()
                        await asyncio.to_thread(proc.wait)
                    elif hasattr(proc, "returncode") and proc.returncode is None:
                        proc.terminate()
                        await proc.wait()  # This is already an asyncio process, so await is correct
                    logger.info(f"Terminated direct process for session {session_id}")
                except Exception as e:
                    logger.error(
                        f"Error terminating process for session {session_id}: {e}"
                    )

            # Remove from tracking using the session_cleanup_callback
            await session_cleanup_callback(session_id)

            return JSONResponse(
                {
                    "status": "success",
                    "message": f"Session {session_id} cleaned up successfully",
                    "process_type": proc_type,
                }
            )

        return JSONResponse(
            status_code=404,
            content={
                "status": "not_found",
                "message": f"Session {session_id} not found",
            },
        )

    except Exception as e:
        logger.error(f"Error cleaning up session {session_id}: {e}")
        return JSONResponse(
            status_code=500, content={"status": "error", "message": str(e)}
        )
