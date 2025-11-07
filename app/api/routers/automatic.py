"""
API Router for the Automatic Voice Agent
"""

import asyncio
import json
import subprocess
import uuid
from typing import Any, Dict

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


async def start_voice_session_internal(
    room_url: str, token: str, session_id: str, **session_params
) -> Dict[str, Any]:
    """
    Internal function to start a voice session with the given parameters.
    Used for automatic session restart during STT fallback scenarios.

    Args:
        room_url: The Daily room URL
        token: The bot token for the room
        session_id: The session ID to use
        **session_params: Additional session parameters

    Returns:
        Dictionary with success status and any error information
    """
    try:
        logger.info(f"Starting internal voice session {session_id}")

        # Store session parameters for potential future fallbacks
        pool = get_voice_agent_pool()
        pool.store_session_parameters(
            session_id, {"room_url": room_url, "token": token, **session_params}
        )

        # Try to get process from pool first
        try:
            voice_process = await pool.get_process(session_id)

            try:
                # Configure the pre-warmed process
                session_config = {
                    "room_url": room_url,
                    "token": token,
                    "session_id": session_id,
                    **session_params,
                }

                config_json = json.dumps(session_config) + "\n"
                voice_process.process.stdin.write(config_json.encode("utf-8"))
                await voice_process.process.stdin.drain()

                logger.info(
                    f"Assigned pre-warmed process {voice_process.process_id} to session {session_id}"
                )

                # Track the process in bot_procs
                bot_procs[voice_process.process.pid] = (
                    voice_process.process,
                    room_url,
                    session_id,
                    "pool",
                )

                return {"success": True, "session_id": session_id}

            except Exception as write_error:
                logger.error(
                    f"Failed to configure pooled process {voice_process.process_id}, returning to pool: {write_error}"
                )
                # Return the process to the pool to prevent a leak
                await pool.return_process(session_id)
                # Re-raise to trigger the fallback mechanism
                raise

        except Exception as e:
            logger.warning(
                f"Failed to get process from pool: {e}, falling back to direct creation"
            )

            # Fallback: Launch subprocess directly
            bot_file = "app.agents.voice.automatic"
            cmd = [
                "python3",
                "-m",
                bot_file,
                "-u",
                room_url,
                "-t",
                token,
                "--session-id",
                session_id,
            ]

            # Add session parameters to command
            arg_map = {
                "client_sid": "--client-sid",
                "mode": "--mode",
                "user_name": "--user-name",
                "user_email": "--user-email",
                "tts_provider": "--tts-provider",
                "voice_name": "--voice-name",
                "euler_token": "--euler-token",
                "breeze_token": "--breeze-token",
                "shop_url": "--shop-url",
                "shop_id": "--shop-id",
                "shop_type": "--shop-type",
                "merchant_id": "--merchant-id",
                "platform_integrations": "--platform-integrations",
                "reseller_id": "--reseller-id",
                "is_fallback_restart": "--is-fallback-restart",
                "original_stt_provider": "--original-stt-provider",
                "fallback_stt_provider": "--fallback-stt-provider",
                "fallback_reason": "--fallback-reason",
            }

            for key, value in session_params.items():
                if value is not None:
                    arg_name = arg_map.get(key)
                    if arg_name:
                        if isinstance(value, list):
                            cmd.extend([arg_name] + value)
                        elif isinstance(value, bool):
                            if value:  # Only add boolean flags if True
                                cmd.append(arg_name)
                        else:
                            cmd.extend([arg_name, str(value)])

            # Start the subprocess
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            # Track the process
            bot_procs[proc.pid] = (proc, room_url, session_id, "direct")

            logger.info(
                f"Started direct process for session {session_id} (PID: {proc.pid})"
            )

            return {"success": True, "session_id": session_id, "process_type": "direct"}

    except Exception as e:
        logger.error(f"Failed to start voice session {session_id}: {e}")
        return {"success": False, "error": str(e)}
