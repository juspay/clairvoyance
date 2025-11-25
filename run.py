import asyncio
import os

import uvicorn
from dotenv import load_dotenv

# Load environment variables from .env file ONCE at startup
load_dotenv()

# Import config and logger first
from app.core.config import HOST, PORT, UVICORN_LOG_LEVEL, UVICORN_RELOAD
from app.core.logger import logger
from app.services.open_feature.dev_cycle.store import initialize_feature_flags

# Initialize DevCycle feature flags at startup (after logger is configured)
asyncio.run(initialize_feature_flags())

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
