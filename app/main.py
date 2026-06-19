import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import __version__
from app.ai.voice.agents.breeze_buddy.chat.cleanup import end_idle_chat_sessions
from app.ai.voice.agents.breeze_buddy.dispatch import (
    clean_stale_bb_locks,
    monitor_dispatch_health,
    reap_stuck_processing_lists,
    reconcile_backlog_to_zset,
    reconcile_channel_tokens,
    start_promoter,
    start_workers,
    stop_promoter,
    stop_workers,
)
from app.ai.voice.agents.breeze_buddy.managers.calls import (
    reconcile_stuck_processing_leads,
)
from app.ai.voice.agents.breeze_buddy.services.agent_router.client import (
    close_smart_router_client,
)

# Database imports
from app.ai.voice.llm._pools import close_all_pools as close_llm_http_pools
from app.api.routers import breeze_buddy, devcycle, feature_flags, systems
from app.api.routers.breeze_buddy.chat import cancel_bus as chat_cancel_bus

# Import background task scheduler
from app.core.background_tasks import BackgroundTaskScheduler
from app.core.config.dynamic import (
    ENABLE_BACKGROUND_TASKS,
)
from app.core.config.static import (
    BACKGROUND_TASKS_LOOP_INTERVAL_SECONDS,
    BB_CLEAN_STALE_LOCKS_INTERVAL_S,
    BB_HEALTH_MONITOR_INTERVAL_S,
    BB_REAP_PROCESSING_INTERVAL_S,
    BB_RECONCILE_BACKLOG_INTERVAL_S,
    BB_RECONCILE_CHANNELS_INTERVAL_S,
    BB_RECONCILE_STUCK_PROCESSING_INTERVAL_S,
    BOT_MAX_DRAIN_SECONDS,
    CHAT_SESSION_END_TIMEOUT_LOOP_INTERVAL_SECONDS,
    CORS_ALLOWED_ORIGINS,
    ENABLE_DISPATCHER,
    ENABLE_SIGTERM_HANDLER,
    HOST,
    POD_ROLE,
    PORT,
)

# Import necessary components from the new structure
from app.core.logger import logger
from app.core.middleware.widget_cors_bypass import CustomWidgetCorsBypassMiddleware
from app.database import close_db_pool, init_db_pool
from app.services.langfuse.tasks.task import initialize_langfuse_tasks
from app.services.redis import (
    close_redis_connections,
    get_redis_service,
    is_redis_configured,
)

# Flag to indicate if pod is draining (no new connections accepted)
_is_draining = False

# Background task scheduler
_background_scheduler = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """FastAPI lifespan manager that handles startup and shutdown tasks."""
    logger.info(f"Application startup... (POD_ROLE={POD_ROLE})")

    # Initialize database and create tables if needed
    try:
        await init_db_pool()
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
    # Initialize Redis client
    try:
        if is_redis_configured():
            redis_service = await get_redis_service()
            await redis_service.get_client()  # Initialize the client
            logger.info("Redis client initialized successfully")
        else:
            logger.info("Redis not configured - skipping Redis initialization")
    except Exception as e:
        logger.error(f"Failed to initialize Redis client: {e}")

    # Start the chat cancel-bus pubsub subscriber. Fans cross-pod cancel
    # publishes out to the local in-flight task registry so the widget
    # Stop button releases the per-session Redis lock immediately instead
    # of waiting on TTL. No-op + warning log if Redis isn't reachable.
    try:
        await chat_cancel_bus.start_subscriber()
    except Exception as e:
        logger.error(f"Failed to start chat cancel-bus subscriber: {e}")

    # DevCycle feature flags are initialized by parent process (run.py) before uvicorn starts
    # Worker processes only need to read from Redis using get_config()
    logger.info(
        "Worker process: DevCycle flags pre-loaded by parent process, reading from Redis"
    )

    # NOTE: The Automatic analytics voice agent has been removed; its
    # functionality is migrated to an Automatic template-driven Breeze
    # Buddy workflow. The Daily room pool and voice-agent process pool
    # existed solely to pre-warm Automatic sessions and were removed with
    # it. Breeze Buddy provisions Daily rooms on demand via its own
    # services/daily helper.

    # Start background task scheduler if enabled
    global _background_scheduler
    if await ENABLE_BACKGROUND_TASKS():
        try:
            # Create scheduler instance with configurable loop interval
            _background_scheduler = BackgroundTaskScheduler(
                loop_interval_seconds=BACKGROUND_TASKS_LOOP_INTERVAL_SECONDS
            )

            # Initialize Langfuse tasks (if configured)
            await initialize_langfuse_tasks(_background_scheduler)

            # Chat-mode idle session cleanup. Distributed lock comes
            # free from the scheduler, so only one pod runs the sweep
            # per interval. See docs/CHAT_MODE.md §7.3.
            _background_scheduler.register_task(
                name="chat_session_idle_cleanup",
                func=end_idle_chat_sessions,
                interval_seconds=CHAT_SESSION_END_TIMEOUT_LOOP_INTERVAL_SECONDS,
            )

            # Event-driven dispatch reconcilers (Plane 5). Only registered on
            # main-server pods; the scheduler's SET NX EX lock further
            # guarantees one-pod-per-tick across the fleet. See
            # docs/BACKLOG_DISPATCHER_REDESIGN.md §2.
            if ENABLE_DISPATCHER:
                _background_scheduler.register_task(
                    name="bb_reconcile_backlog_to_zset",
                    func=reconcile_backlog_to_zset,
                    interval_seconds=BB_RECONCILE_BACKLOG_INTERVAL_S,
                )
                _background_scheduler.register_task(
                    name="bb_reap_stuck_processing_lists",
                    func=reap_stuck_processing_lists,
                    interval_seconds=BB_REAP_PROCESSING_INTERVAL_S,
                )
                _background_scheduler.register_task(
                    name="bb_reconcile_channel_tokens",
                    func=reconcile_channel_tokens,
                    interval_seconds=BB_RECONCILE_CHANNELS_INTERVAL_S,
                )
                _background_scheduler.register_task(
                    name="bb_clean_stale_locks",
                    func=clean_stale_bb_locks,
                    interval_seconds=BB_CLEAN_STALE_LOCKS_INTERVAL_S,
                )
                # Stuck-PROCESSING reconciler: closes leads whose call-end
                # webhook never arrived. Inherited from the deleted cron
                # path; same 10-min staleness threshold.
                _background_scheduler.register_task(
                    name="bb_reconcile_stuck_processing_leads",
                    func=reconcile_stuck_processing_leads,
                    interval_seconds=BB_RECONCILE_STUCK_PROCESSING_INTERVAL_S,
                )
                # Health monitor: alert-only periodic check for no-leader /
                # dispatch-halted / schedule-depth-high. Slack alerts are
                # throttled by the alerts module; this task is safe at 60s.
                _background_scheduler.register_task(
                    name="bb_monitor_dispatch_health",
                    func=monitor_dispatch_health,
                    interval_seconds=BB_HEALTH_MONITOR_INTERVAL_S,
                )

            # Start the scheduler only if tasks are registered
            if _background_scheduler.tasks:
                await _background_scheduler.start()
                logger.info("Background task scheduler started")
            else:
                logger.info("No background tasks registered, scheduler not started")
        except Exception as e:
            logger.error(f"Failed to start background task scheduler: {e}")
    else:
        logger.info(
            "Background task scheduler disabled (ENABLE_BACKGROUND_TASKS=false)"
        )

    # Start the event-driven dispatcher (promoter + workers). Main-server
    # workload only by default. Channel-semaphore init is handled by the
    # reconciler — no separate boot-time initialisation logic. See §2.
    if ENABLE_DISPATCHER:
        try:
            # Fast cold-start: trigger one reconcile_channel_tokens immediately
            # so workers don't BLPOP on empty channel LISTs while waiting for
            # the periodic reconciler tick. Share the same SET NX EX lock key
            # the BackgroundTaskScheduler uses for the periodic tick — that
            # way the boot reconcile and the first scheduled tick can't run
            # concurrently and double-topup token counts.
            try:
                redis_svc = await get_redis_service()
                acquired = await redis_svc.set(
                    key="background:task:bb_reconcile_channel_tokens:lock",
                    value="locked",
                    nx=True,
                    ex=BB_RECONCILE_CHANNELS_INTERVAL_S,
                )
                if acquired:
                    await reconcile_channel_tokens()
                else:
                    logger.info(
                        "Boot-time channel-tokens reconcile already running on "
                        "another pod; skipping (scheduled tick will heal)."
                    )
            except Exception as e:
                logger.warning(
                    f"Initial channel-tokens reconcile failed (will retry "
                    f"on scheduled tick): {e}"
                )

            await start_promoter()
            await start_workers()
            logger.info("Event-driven dispatcher started")
        except Exception as e:
            logger.error(f"Failed to start event-driven dispatcher: {e}", exc_info=True)
    else:
        logger.info("Event-driven dispatcher disabled (ENABLE_DISPATCHER=false)")

    yield

    logger.info("Application shutdown event triggered...")

    # Stop the event-driven dispatcher before scheduler/db close so any
    # in-flight workers get their locks/tokens released cleanly.
    if ENABLE_DISPATCHER:
        try:
            logger.info("Stopping event-driven dispatcher...")
            await stop_workers()
            await stop_promoter()
        except Exception as e:
            logger.error(f"Error stopping dispatcher: {e}", exc_info=True)

    # Stop background task scheduler if running
    if _background_scheduler:
        logger.info("Stopping background task scheduler...")
        await _background_scheduler.stop()

    # Stop chat cancel-bus subscriber
    try:
        await chat_cancel_bus.stop_subscriber()
    except Exception as e:
        logger.error(f"Error stopping chat cancel-bus subscriber: {e}")

    # Graceful drain period - wait for active sessions to complete if enabled
    if ENABLE_SIGTERM_HANDLER and _is_draining:
        logger.info(
            f"Drain mode active. Waiting {BOT_MAX_DRAIN_SECONDS}s for active sessions to complete..."
        )
        await asyncio.sleep(BOT_MAX_DRAIN_SECONDS)
        logger.info(
            f"Drain period ({BOT_MAX_DRAIN_SECONDS}s) complete. Proceeding with cleanup."
        )

    # Close Smart Router client (release HTTP connection pool)
    await close_smart_router_client()

    # Close shared httpx pools used by chat LLM clients (Azure today).
    # Drains keep-alive connections cleanly so we don't leak fds on SIGTERM.
    await close_llm_http_pools()
    # Close database pool
    await close_db_pool()
    # Close Redis connections
    await close_redis_connections()


app = FastAPI(title="Breeze Automatic Server", version=__version__, lifespan=lifespan)

# CORS — path-aware. The strict static allowlist (CORS_ALLOWED_ORIGINS)
# still gates admin/RBAC/portal traffic, but the public widget surface
# (/agent/voice/breeze-buddy/widget/…) bypasses it: those routes carry
# their own permissive @router.options handlers and enforce per-merchant
# allowed_origins inside the POST handler via widget_config. Without
# this bypass, onboarding each new merchant would require editing this
# env var and a rolling restart — defeating the whole point of the
# per-widget allowlist.
app.add_middleware(
    CustomWidgetCorsBypassMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(
    breeze_buddy.router, prefix="/agent/voice/breeze-buddy", tags=["Breeze Buddy"]
)
app.include_router(devcycle.router, prefix="", tags=["DevCycle"])
app.include_router(
    feature_flags.router,
    prefix="/agent/voice/breeze-buddy",
    tags=["Feature Flags"],
)

# System health endpoints
app.include_router(systems.router, prefix="", tags=["Systems"])


# Root endpoint - health check
@app.get("/")
async def health_check():
    """
    Root endpoint - health check.

    Returns basic service information and status.
    """
    return {
        "service": "Clairvoyance API",
        "version": __version__,
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# Drain endpoint for Kubernetes preStop hook
@app.get("/drain")
async def drain():
    """Called by Kubernetes preStop hook before sending SIGTERM"""
    global _is_draining
    logger.info("Drain endpoint called by Kubernetes - marking pod as draining")
    _is_draining = True
    return JSONResponse({"status": "draining"})


# The main block is now only for direct execution, which is not the recommended way.
# Uvicorn running from run.py is the standard.
if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
