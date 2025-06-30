import uvicorn
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from app.core.logger import logger

from app.core.config import PORT, HOST, UVICORN_RELOAD, UVICORN_LOG_LEVEL

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
        access_log=True   # Keep access logs but route through our interceptor
    )