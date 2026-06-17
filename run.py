# STEP 1: Load environment variables VERY FIRST - before any other imports
from dotenv import load_dotenv

load_dotenv()

# STEP 2: Now safe to import everything else
import uvicorn

# STEP 3: Now safe to import config and logger
from app.core.config.static import (
    HOST,
    PORT,
    UVICORN_LOG_LEVEL,
    UVICORN_RELOAD,
)
from app.core.logger import logger

if __name__ == "__main__":

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
