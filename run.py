# STEP 1: Load environment variables VERY FIRST - before any other imports
from dotenv import load_dotenv

load_dotenv()

# STEP 2: Now safe to import everything else
import asyncio

import uvicorn

# STEP 3: Now safe to import config and logger
from app.core.config.static import (
    ENABLE_REDIS_DYNAMIC_CONFIG,
    HOST,
    PORT,
    UVICORN_LOG_LEVEL,
    UVICORN_RELOAD,
)
from app.core.logger import logger
from app.services.live_config.store import initialize_feature_flags
from app.services.redis import (
    close_redis_connections,
    get_redis_service,
    is_redis_configured,
)


async def initialize_devcycle():
    """Initialize DevCycle and populate Redis before uvicorn starts.

    This ensures all worker processes and subprocesses can read flags from Redis
    without needing to call DevCycle API themselves.
    """
    try:
        if not ENABLE_REDIS_DYNAMIC_CONFIG:
            logger.info(
                "Parent process: ENABLE_REDIS_DYNAMIC_CONFIG is False - "
                "skipping Redis/DevCycle init, all dynamic config will use environment variables only"
            )
        elif is_redis_configured():
            # Initialize Redis connection
            redis_service = await get_redis_service()
            await redis_service.get_client()  # Initialize the client
            logger.info("Parent process: Redis connection established")

            # Initialize DevCycle and populate Redis
            await initialize_feature_flags()
            logger.info(
                "Parent process: DevCycle feature flags initialized and stored in Redis"
            )

            # Close Redis connection (workers will create their own)
            await close_redis_connections()
            logger.info(
                "Parent process: Redis connection closed (workers will create new connections)"
            )
        else:
            logger.warning(
                "Parent process: Redis not configured - DevCycle flags will use environment variables only"
            )
    except Exception as e:
        logger.error(f"Parent process: Failed to initialize DevCycle: {e}")
        logger.warning("Parent process: Continuing with environment variable fallback")


if __name__ == "__main__":
    # STEP 4: Initialize DevCycle in parent process before starting uvicorn
    logger.info("Parent process: Initializing DevCycle before starting workers...")
    asyncio.run(initialize_devcycle())

    # STEP 5: Start uvicorn server (will spawn worker processes)
    logger.info(f"Starting Uvicorn server on {HOST}:{PORT}")
    logger.info(f"Reload enabled: {UVICORN_RELOAD}")
    logger.info(f"Log level: {UVICORN_LOG_LEVEL}")
    logger.info("Running the main application.")
    uvicorn.run(
        "app.main:app",  # Path to the FastAPI app object in app/main.py
        host=HOST,
        port=PORT,
        reload=UVICORN_RELOAD,
        log_level=UVICORN_LOG_LEVEL,
        log_config=None,  # Disable Uvicorn's default logging config
        access_log=True,  # Keep access logs but route through our interceptor
    )
