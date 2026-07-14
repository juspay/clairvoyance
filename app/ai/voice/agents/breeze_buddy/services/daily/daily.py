"""
Daily transport service for Breeze Buddy.

This module handles Daily (web-based) voice session infrastructure:
- Room creation and token management
- Starting the bot for Daily sessions
- Call completion handling for Daily mode
"""

import asyncio
import sys
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from pipecat.runner.types import DailyRunnerArguments
from pipecat.transports.daily.utils import (
    DailyMeetingTokenParams,
    DailyMeetingTokenProperties,
    DailyRESTHelper,
    DailyRoomParams,
    DailyRoomProperties,
)

from app.ai.voice.agents.breeze_buddy.agent import daily_bot
from app.ai.voice.agents.breeze_buddy.services.daily.launch_payload import (
    BotLaunchPayload,
)
from app.core.config.dynamic import BB_DAILY_BOT_SUBPROCESS
from app.core.config.static import (
    BB_DAILY_BOT_MAX_LIFETIME_SECS,
    BB_MAX_CONCURRENT_DAILY_BOTS,
    BREEZE_BUDDY_DAILY_API_KEY,
    BREEZE_BUDDY_DAILY_API_URL,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    update_lead_call_completion_details,
    update_lead_call_id_by_id,
    update_lead_call_recording_url,
)
from app.schemas import LeadCallStatus, LeadCallTracker


async def daily_completion_function(
    call_id: str,
    outcome: Optional[str] = None,
    call_end_time: Optional[datetime] = None,
    meta_data: Optional[dict] = None,
) -> Optional[LeadCallTracker]:
    """Completion function for Daily mode - updates lead status to FINISHED.

    For Daily mode, the call_id is actually the lead_id since Daily doesn't have
    a traditional call_sid like telephony providers.
    """
    logger.info(f"Daily completion: updating lead {call_id} to FINISHED")
    return await update_lead_call_completion_details(
        id=call_id,
        status=LeadCallStatus.FINISHED,
        outcome=outcome,
        meta_data=meta_data,
        call_end_time=call_end_time,
    )


# Strong references to live bot work: reaper tasks for subprocess bots and
# the bot task itself for the legacy in-process launch. asyncio only keeps
# weak references to tasks, so a bare create_task could be garbage-collected
# mid-flight (Ruff RUF006). Doubles as the live-bot counter behind
# BB_MAX_CONCURRENT_DAILY_BOTS — each entry is exactly one live call.
_live_bot_tasks: set = set()


def _track_live_bot(task: "asyncio.Task") -> None:
    _live_bot_tasks.add(task)
    task.add_done_callback(_live_bot_tasks.discard)


async def _reap_bot_process(proc: asyncio.subprocess.Process, lead_id: str) -> None:
    """Await a bot subprocess and log its exit — no state changes.

    Log-only on normal exits: a dead bot is covered by the same safety nets
    that covered a dead in-process bot task before process isolation (the
    /voice/end crash-net channel flip and the 1h Daily room expiry).

    The lifetime kill is a last-resort watchdog: a healthy call can never
    outlive the 1h Daily room expiry, so a child alive past
    BB_DAILY_BOT_MAX_LIFETIME_SECS is wedged (e.g. a stuck daily-python
    native thread that also hangs pipecat's idle-timeout teardown) and would
    otherwise leak the process and its Postgres/Redis connections forever.
    """
    try:
        returncode = await asyncio.wait_for(
            proc.wait(), timeout=BB_DAILY_BOT_MAX_LIFETIME_SECS
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"Daily bot subprocess for lead {lead_id} (pid={proc.pid}) exceeded "
            f"BB_DAILY_BOT_MAX_LIFETIME_SECS={BB_DAILY_BOT_MAX_LIFETIME_SECS}s — "
            "killing wedged child"
        )
        try:
            proc.kill()
        except ProcessLookupError:
            pass  # exited between the timeout and the kill
        returncode = await proc.wait()
    if returncode == 0:
        logger.info(f"Daily bot subprocess for lead {lead_id} exited cleanly")
    else:
        logger.warning(
            f"Daily bot subprocess for lead {lead_id} exited with code {returncode}"
        )


async def _launch_daily_bot(runner_args: DailyRunnerArguments) -> None:
    """Spawn ``bot_runner`` as the call's own OS process.

    Per-call process isolation keeps the audio pipeline (and the chat brain
    it drives in stream mode) away from API-traffic event-loop stalls — the
    widget voice crackle root cause — and contains daily-python native
    crashes to one call. The child runs the exact same ``daily_bot`` code;
    all bot↔API coordination is via Postgres/Redis/the Daily room, so
    behavior is identical to the historical in-process launch.

    ``BB_DAILY_BOT_SUBPROCESS=false`` (dynamic config: DevCycle/Redis or env,
    escape hatch) falls back to the legacy in-process asyncio-task launch.
    """
    # `or {}` / `or ""` narrow pipecat's Optional field types;
    # start_daily_session always sets token and builds body around lead_id.
    body = runner_args.body or {}
    lead_id = body["lead_id"]
    if not await BB_DAILY_BOT_SUBPROCESS():
        # Legacy in-process launch. The aiohttp session lives for the bot's
        # lifetime (global HTTP functions); daily_bot's finally closes it.
        bot_aiohttp_session = create_aiohttp_session()
        bot_task = asyncio.create_task(
            daily_bot(runner_args, daily_completion_function, bot_aiohttp_session)
        )
        _track_live_bot(bot_task)
        logger.info(
            f"Started in-process Daily bot for lead_id: {lead_id} "
            "(BB_DAILY_BOT_SUBPROCESS=false)"
        )
        return

    payload = BotLaunchPayload(
        room_url=runner_args.room_url,
        token=runner_args.token or "",
        body=body,
    ).model_dump_json()
    # Payload goes over stdin, never argv: the Daily bot token must not be
    # visible in `ps` output. stdout/stderr are inherited so the child's
    # loguru output lands in the same container log stream; the environment
    # is inherited too — the child sizes its own small DB pool explicitly in
    # bot_runner via init_db_pool(min_size=BB_VOICE_BOT_DB_POOL_SIZE, ...).
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.ai.voice.agents.breeze_buddy.services.daily.bot_runner",
        stdin=asyncio.subprocess.PIPE,
    )
    # Reap unconditionally from here on: even if the stdin handoff below
    # fails (e.g. the child died on startup), the process must still be
    # awaited so it never lingers as a zombie.
    _track_live_bot(asyncio.create_task(_reap_bot_process(proc, lead_id)))
    assert proc.stdin is not None  # PIPE above guarantees it
    try:
        proc.stdin.write(payload.encode())
        await proc.stdin.drain()
    finally:
        # Always signal EOF — bot_runner blocks on stdin.read() and a
        # half-written payload must fail its parse instead of hanging it.
        proc.stdin.close()
    logger.info(f"Spawned Daily bot subprocess (pid={proc.pid}) for lead_id: {lead_id}")


async def start_daily_session(
    lead_id: str, *, enable_recording: bool = True
) -> Dict[str, Any]:
    """Create a Daily room and start the bot for a lead.

    This function handles the infrastructure setup for Daily sessions:
    1. Generates a unique session ID
    2. Creates a Daily room with appropriate settings
    3. Generates tokens for both user and bot
    4. Starts the bot in its own OS process (legacy in-process launch via
       BB_DAILY_BOT_SUBPROCESS=false)

    Args:
        lead_id: The ID of the lead to start the session for
        enable_recording: When True (default — preserves existing
            telephony / demo behaviour), enable Daily cloud recording
            and set the recording_url sentinel so the dashboard player
            renders. The unified widget router (CHAT_MODE.md §14)
            passes False to skip recording entirely for widget voice
            attachments — the lead is reused across attachments and a
            single recording_url field cannot represent N
            recordings, so we drop the feature for widget v1.

    Returns:
        Dict containing room_url, token (for user), session_id, and lead_id
    """
    # Reject BEFORE creating any room/DB state: each live bot is its own OS
    # process holding dedicated Postgres/Redis connections and ~300MB RSS, so
    # an unbounded spike would exhaust shared infrastructure (Postgres
    # max_connections) that the old shared-pool in-process launch never
    # touched. The count is approximate (no lock) — the cap is a safety rail,
    # not an exact scheduler.
    live_bots = len(_live_bot_tasks)
    if live_bots >= BB_MAX_CONCURRENT_DAILY_BOTS:
        raise RuntimeError(
            f"Daily bot capacity reached ({live_bots} live bots >= "
            f"BB_MAX_CONCURRENT_DAILY_BOTS={BB_MAX_CONCURRENT_DAILY_BOTS}); "
            "rejecting new voice session"
        )

    # Generate session ID
    session_id = str(uuid.uuid4())

    # Create Daily room on-demand
    async with create_aiohttp_session() as aiohttp_session:
        daily_rest = DailyRESTHelper(
            daily_api_key=BREEZE_BUDDY_DAILY_API_KEY,
            daily_api_url=BREEZE_BUDDY_DAILY_API_URL,
            aiohttp_session=aiohttp_session,
        )

        # Create room with params. Cloud recording is opt-in per call so
        # widget voice attachments can skip it (no per-call recording
        # surface — the lead is reused across attachments).
        room_properties_kwargs: Dict[str, Any] = {
            "exp": time.time() + 3600,  # 1 hour expiry
            "eject_at_room_exp": True,
        }
        if enable_recording:
            room_properties_kwargs["enable_recording"] = "cloud"
        room_params = DailyRoomParams(
            properties=DailyRoomProperties(**room_properties_kwargs)
        )
        room = await daily_rest.create_room(room_params)
        room_url = room.url

        # Create tokens
        user_token = await daily_rest.get_token(room_url)
        if enable_recording:
            bot_token = await daily_rest.get_token(
                room_url,
                expiry_time=3600,
                params=DailyMeetingTokenParams(
                    properties=DailyMeetingTokenProperties(
                        start_cloud_recording=True,
                    )
                ),
            )
        else:
            bot_token = await daily_rest.get_token(room_url, expiry_time=3600)

    # Store room name as call_id for on-demand recording retrieval.
    # Always set call_id (it's the call SID equivalent for Daily mode
    # and feeds end_conversation lookups), but only set the
    # recording_url sentinel when recording is actually enabled — a
    # widget call with no recording shouldn't show a player.
    updated_lead = await update_lead_call_id_by_id(lead_id, room.name)
    if not updated_lead:
        logger.warning(
            f"Failed to set call_id for lead {lead_id} — completion lookup may not work"
        )
    elif enable_recording:
        # Sentinel recording_url so frontend shows the recording player
        # (depends on call_id being set above).
        recording_lead = await update_lead_call_recording_url(room.name, "daily")
        if not recording_lead:
            logger.warning(
                f"Failed to set recording_url for lead {lead_id} — frontend may not show recording player"
            )
    else:
        # Clear any stale sentinel from a prior recorded attachment on this
        # same lead. Widget voice reuses leads across attachments, so a
        # previous run that set ``recording_url="daily"`` would otherwise
        # leave a player visible on this non-recorded session.
        cleared_lead = await update_lead_call_recording_url(room.name, "")
        if not cleared_lead:
            logger.warning(
                f"Failed to clear stale recording_url for lead {lead_id} — "
                "frontend may still show a stale recording player"
            )

    logger.info(
        f"Created Daily room for Breeze Buddy session {session_id}: {room_url} "
        f"(recording={'on' if enable_recording else 'off'})"
    )

    # Prepare runner arguments for Daily transport
    runner_args = DailyRunnerArguments(
        room_url=room_url,
        token=bot_token,
        body={
            "lead_id": lead_id,
            "session_id": session_id,
        },
    )

    # Start the bot in its own OS process (see _launch_daily_bot). Unlike the
    # old create_task launch this CAN fail (fork error, child dying before
    # reading stdin) — and the room/call_id/recording-sentinel writes above
    # are already committed, so roll the sentinel back: neither the dashboard
    # handler nor the demo abort path clears it, and a lead whose bot never
    # spawned must not render a recording player for a recording that can
    # never exist. call_id is left in place — a retry overwrites it.
    try:
        await _launch_daily_bot(runner_args)
    except Exception:
        if enable_recording:
            try:
                await update_lead_call_recording_url(room.name, "")
            except Exception as cleanup_exc:
                logger.warning(
                    f"Failed to clear recording_url after failed bot launch "
                    f"for lead {lead_id}: {cleanup_exc}"
                )
        raise

    logger.info(
        f"Successfully started Breeze Buddy Daily bot for lead_id: {lead_id}, session: {session_id}"
    )

    # Return room credentials to caller
    return {
        "room_url": room_url,
        "token": user_token,
        "session_id": session_id,
        "lead_id": lead_id,
    }
