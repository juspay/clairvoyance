import sys
import json
import logging
from loguru import logger

# Remove the default sink to have full control over logging.
logger.remove()

from app.core.config import ENVIRONMENT, PROD_LOG_LEVEL

LOGS_TO_UPLEVEL = ["pipecat.transports.base_input", "pipecat.transports.base_output"]

def log_record_filter(record):
    if record["name"] in LOGS_TO_UPLEVEL and record["level"].name == "DEBUG":
        info_level = logger.level("INFO")
        record["level"].name = info_level.name
        record["level"].no = info_level.no
        record["level"].icon = info_level.icon
    return True

def json_sink(message):
    """
    Custom sink function for JSON output in production environments.
    This enables structured logging for log aggregation systems like ELK, Grafana, Datadog.
    """
    record = message.record
    log_entry = {
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "logger": record["name"],
        "function": record["function"],
        "line": record["line"],
        "message": record["message"],
        "module": record["module"],
        "process": record["process"].id if record["process"] else None,
        "thread": record["thread"].id if record["thread"] else None,
        **record["extra"],  # Include any additional context data
    }
    print(json.dumps(log_entry))

class InterceptHandler(logging.Handler):
    """
    Intercept standard logging messages toward Loguru sinks.
    This allows us to capture logs from libraries like Uvicorn.
    """
    def emit(self, record):
        # Get corresponding Loguru level if it exists
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

def _setup_logger_sinks(include_session_id: bool = False):
    """
    Internal function to set up logger sinks based on environment.
    Reduces code duplication between initial setup and session configuration.
    """
    if ENVIRONMENT == "dev":
        # Development mode format
        session_part = "<cyan>[{extra[session_id]}]</cyan> | " if include_session_id else ""
        stdout_fmt = (
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            f"{session_part}"
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        )
        stderr_fmt = stdout_fmt.replace("<green>", "<red>").replace("</green>", "</red>")
        
        logger.add(
            sys.stdout,
            level="DEBUG",
            format=stdout_fmt,
            enqueue=True,
            backtrace=False,
            colorize=True,
        )
        
        logger.add(
            sys.stderr,
            level="WARNING",
            format=stderr_fmt,
            enqueue=True,
            backtrace=True,
            colorize=True,
        )
    else:
        # Production mode - JSON automatically includes session_id from extra
        logger.add(
            json_sink,
            level=PROD_LOG_LEVEL,  # Configurable log level via PROD_LOG_LEVEL env var
            enqueue=True,
            backtrace=False,  # Keep JSON logs concise and predictable
            diagnose=False,   # Prevent sensitive data leakage and performance overhead
            filter=log_record_filter,
        )

def configure_session_logger(session_id: str):
    """
    Configure the logger to automatically include session_id in all log entries.
    This should be called once at the start of a subprocess.
    """
    logger.remove()
    _setup_logger_sinks(include_session_id=True)
    logger.configure(extra={"session_id": session_id})
    # Also set up logging interception for session-based logging
    setup_logging_interception()

def setup_logging_interception():
    """
    Set up interception of all Python standard logging calls.
    This ensures that logs from libraries like Uvicorn are formatted consistently.
    """
    # Intercept everything at the root logger level
    logging.root.handlers = [InterceptHandler()]
    logging.root.setLevel(logging.DEBUG)

    # Remove every other logger's handlers and propagate to root logger
    for name in logging.root.manager.loggerDict.keys():
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True

# Initial logger configuration
_setup_logger_sinks(include_session_id=False)

# Set up logging interception for unified logging
setup_logging_interception()

# Export the configured logger for use throughout the application.
__all__ = ["logger", "configure_session_logger"]