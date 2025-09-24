import os

import uvicorn
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from app.core.config import config
from app.core.logger import logger

if __name__ == "__main__":
    logger.info(f"Starting Uvicorn server on {config.Server.HOST}:{config.Server.PORT}")
    logger.info(f"Reload enabled: {config.Server.UVICORN_RELOAD}")
    logger.info(f"Log level: {config.Server.UVICORN_LOG_LEVEL}")
    logger.info("Running the main application.")
    uvicorn.run(
        "app.main:app",  # Path to the FastAPI app object in app/main.py
        host=config.Server.HOST,
        port=config.Server.PORT,
        reload=config.Server.UVICORN_RELOAD,
        log_level=config.Server.UVICORN_LOG_LEVEL,
        log_config=None,  # Disable Uvicorn's default logging config
        access_log=True,  # Keep access logs but route through our interceptor
    )
