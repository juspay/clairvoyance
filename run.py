import uvicorn
import os
import logging

# Configure logging for the run script itself
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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
        log_level=log_level
    )