import json
import logging
import sys

from loguru import logger

# Remove the default sink to have full control over logging.
logger.remove()

from app.core.config import ENABLE_OTEL_LOGGING, ENVIRONMENT, PROD_LOG_LEVEL

# Import OTEL functions from separate module
try:
    from app.core.logger.otel import OTEL_AVAILABLE, get_otel_handler, get_otel_logger
except ImportError:
    OTEL_AVAILABLE = False
    get_otel_logger = lambda: None
    get_otel_handler = lambda: None


def otel_sink(message):
    """Custom OTEL sink that preserves full structured data like JSON sink"""
    if not ENABLE_OTEL_LOGGING or not OTEL_AVAILABLE:
        return

    # Prevent recursive logging of OTEL export errors
    if "Exception while exporting" in str(message.record.get("message", "")):
        return

    otel_logger = get_otel_logger()
    if not otel_logger:
        return

    otel_handler = get_otel_handler()
    if not otel_handler:
        return

    try:
        record = message.record

        # Create the full structured data like your JSON sink
        structured_data = {
            "timestamp": record["time"].isoformat(),
            "level": record["level"].name,
            "logger": record["name"],
            "function": record["function"],
            "line": record["line"],
            "message": record["message"],
            "module": record["module"],
            "process": record["process"].id if record["process"] else None,
            "thread": record["thread"].id if record["thread"] else None,
        }

        # Add session context if available
        if record["extra"]:
            for key, value in record["extra"].items():
                if value is not None:
                    structured_data[key] = value

        # Create LogRecord with the full structured data as the formatted message
        log_record = logging.LogRecord(
            name=structured_data["logger"],
            level=getattr(logging, structured_data["level"], logging.INFO),
            pathname="",
            lineno=structured_data["line"],
            msg=json.dumps(structured_data),  # Put full JSON as the message itself
            args=(),
            exc_info=None,
        )

        # Set the timestamp to match our original log
        log_record.created = record["time"].timestamp()

        # Emit through the OTEL handler
        otel_handler.emit(log_record)

    except Exception as e:
        # Don't let OTEL issues break the application
        pass


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

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def _setup_logger_sinks(
    include_session_id: bool = False, include_client_sid: bool = False
):
    """
    Internal function to set up logger sinks based on environment.
    Reduces code duplication between initial setup and session configuration.
    """

    # Filter function to completely block websockets, daily_core, and specific openai spam logs
    def filter_spam_logs(record):
        logger_name = record["name"]
        return not (
            logger_name.startswith("websockets")
            or logger_name.startswith("daily_core")
            or logger_name.startswith(
                "openai._base_client"
            )  # Only block _base_client logs, not all openai logs
        )

    if ENVIRONMENT == "dev":
        # Development mode format
        session_part = (
            "<cyan>[{extra[session_id]}]</cyan> | " if include_session_id else ""
        )
        client_sid_part = (
            "<yellow>[{extra[client_sid]}]</yellow> | " if include_client_sid else ""
        )
        stdout_fmt = (
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            f"{session_part}"
            f"{client_sid_part}"
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        )
        stderr_fmt = stdout_fmt.replace("<green>", "<red>").replace(
            "</green>", "</red>"
        )

        logger.add(
            sys.stdout,
            level="DEBUG",
            format=stdout_fmt,
            filter=filter_spam_logs,  # Apply filter to block spam logs
            enqueue=True,
            backtrace=False,
            colorize=True,
        )

        logger.add(
            sys.stderr,
            level="WARNING",
            format=stderr_fmt,
            filter=filter_spam_logs,  # Apply filter to block spam logs
            enqueue=True,
            backtrace=True,
            colorize=True,
        )
    else:
        # Production mode - JSON automatically includes session_id and client_sid from extra
        logger.add(
            json_sink,
            level=PROD_LOG_LEVEL,  # Configurable log level via PROD_LOG_LEVEL env var defaulting to INFO
            enqueue=True,
            backtrace=False,  # Keep JSON logs concise and predictable
            diagnose=False,  # Prevent sensitive data leakage and performance overhead
        )

        # NEW: Add OTEL custom sink in production only
        if ENABLE_OTEL_LOGGING:
            logger.add(
                otel_sink,  # Use custom sink that preserves structured data
                level=PROD_LOG_LEVEL,
                enqueue=True,
                backtrace=False,
                diagnose=False,
            )
            logger.info(
                "OTEL custom sink added - structured logs will be sent to collector"
            )

        DEBUG_LOGS_TO_UPLEVEL = {
            "pipecat.transports.base_input",
            "pipecat.transports.base_output",
        }

        # 2) Secondary "promote" sink for exactly those two DEBUG records
        def promote_debug_logs(record):
            name = record["name"]
            lvl = record["level"].name
            # target only required debug logs
            if name in DEBUG_LOGS_TO_UPLEVEL and lvl == "DEBUG":
                # bump them up to INFO so that they pass the PROD_LOG_LEVEL filter
                record["level"].name = "INFO"
                record["level"].no = logger.level("INFO").no
                return True
            return False

        logger.add(
            json_sink,  # same JSON formatter
            level="DEBUG",  # catch DEBUGs…
            filter=promote_debug_logs,
            enqueue=True,
            backtrace=False,
            diagnose=False,
        )


def configure_session_logger(session_id: str, client_sid: str = None):
    """
    Configure the logger to automatically include session_id and client_sid in all log entries.
    This should be called once at the start of a subprocess.
    """
    logger.remove()
    _setup_logger_sinks(include_session_id=True, include_client_sid=bool(client_sid))

    extra_context = {"session_id": session_id}
    if client_sid:
        extra_context["client_sid"] = client_sid

    logger.configure(extra=extra_context)
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

    # Completely disable logs from websockets and daily_core to avoid spamming
    logging.getLogger("websockets").disabled = True
    logging.getLogger("daily_core").disabled = True


# Initial logger configuration
_setup_logger_sinks(include_session_id=False)

# Set up logging interception for unified logging
setup_logging_interception()

# Export the configured logger for use throughout the application.
__all__ = ["logger", "configure_session_logger"]
