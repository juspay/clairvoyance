import uvicorn
import os
import logging

# Configure logging for the run script itself
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Import the configuration flag
from app.core.config import ENABLE_SIMPLE_V2_FLOW

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0") # Default to 0.0.0.0 to be accessible externally
    reload = os.environ.get("UVICORN_RELOAD", "true").lower() == "true" # Enable reload by default for dev
    log_level = os.environ.get("UVICORN_LOG_LEVEL", "info")

    logger.info(f"Starting Uvicorn server on {host}:{port}")
    logger.info(f"Reload enabled: {reload}")
    logger.info(f"Log level: {log_level}")

    if ENABLE_SIMPLE_V2_FLOW:
        logger.info("ENABLE_SIMPLE_V2_FLOW is True. Running the v2 application.")
        # Ensure v2.py can be imported and its 'app' is accessible
        # The app instance in v2.py is named 'app'
        uvicorn.run(
            "v2:app",  # Path to the FastAPI app object in v2.py
            host=host,
            port=port,
            reload=reload,
            log_level=log_level
        )
    else:
        logger.info("ENABLE_SIMPLE_V2_FLOW is False. Running the main application.")
        uvicorn.run(
            "app.main:app",  # Path to the FastAPI app object in app/main.py
            host=host,
            port=port,
            reload=reload,
            log_level=log_level
        )