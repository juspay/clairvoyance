import uvicorn
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from app.core.logger import logger

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0") # Default to 0.0.0.0 to be accessible externally
    reload = os.environ.get("UVICORN_RELOAD", "true").lower() == "true" # Enable reload by default for dev
    log_level = os.environ.get("UVICORN_LOG_LEVEL", "info")

    logger.info(f"Starting Uvicorn server on {host}:{port}")
    logger.info(f"Reload enabled: {reload}")
    logger.info(f"Log level: {log_level}")
    logger.info("Running the main application.")
    uvicorn.run(
        "app.main:app",  # Path to the FastAPI app object in app/main.py
        host=host,
        port=port,
        reload=reload,
        log_level=log_level,
        log_config=None,  # Disable Uvicorn's default logging config
        access_log=True   # Keep access logs but route through our interceptor
    )