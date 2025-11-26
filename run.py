# STEP 1: Load environment variables VERY FIRST - before any other imports
from dotenv import load_dotenv

load_dotenv()

# STEP 2: Now safe to import everything else
import asyncio
import os

import uvicorn

# STEP 3: Initialize DevCycle feature flags (after env loaded)
from app.services.live_config.store import initialize_feature_flags

asyncio.run(initialize_feature_flags())

# STEP 4: Now safe to import config and logger (after feature flags initialized)
from app.core.config.static import HOST, PORT, UVICORN_LOG_LEVEL, UVICORN_RELOAD
from app.core.logger import logger

if __name__ == "__main__":
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
