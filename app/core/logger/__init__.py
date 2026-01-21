import json
import logging
import sys
from typing import Optional

from loguru import logger

# Remove the default sink to have full control over logging.
logger.remove()

# Use environment variables directly to avoid circular import
from app.core.config.static import ENVIRONMENT, PROD_LOG_LEVEL
from app.core.logger.context import get_log_context


# Patcher to inject log context into extra BEFORE enqueueing
# This is critical because enqueue=True processes logs in a background thread
# where contextvars are not propagated. The patcher runs in the calling thread.
# Defined at module level so it can be reused in configure_session_logger()
def log_context_patcher(record):
    """Inject log context into record['extra'] for both dev and prod formatting."""
    ctx = get_log_context()
    record["extra"]["_log_context"] = ctx


def json_sink(message):
    """
    Custom sink function for JSON output in production environments.
    This enables structured logging for log aggregation systems like ELK, Grafana, Datadog.

    Log context fields are injected by the patcher into record['extra']['_log_context']
    BEFORE enqueueing, so they're available here.
    """
    record = message.record
    extra = record["extra"]

    # Build log entry
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
    }

    # Add log context fields (injected by patcher)
    log_ctx = extra.pop("_log_context", {})
    for key, value in log_ctx.items():
        if (
            value is not None
        ):  # Only exclude None; keep falsy-but-valid values like 0, False, and ""
            log_entry[key] = value

    # Add any other extra fields
    for key, value in extra.items():
        log_entry[key] = value

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
        frame = logging.currentframe()
        depth = 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            next_frame = frame.f_back
            if next_frame is None:
                break
            frame = next_frame
            depth += 1

        # Bind log context to forwarded logs
        # This ensures pipecat/library logs also get the log context
        log_context = get_log_context()
        logger.opt(depth=depth, exception=record.exc_info).bind(
            _log_context=log_context
        ).log(level, record.getMessage())


def _setup_logger_sinks(
    include_session_id: bool = False, include_client_sid: bool = False
):
    """
    Internal function to set up logger sinks based on environment.
    Reduces code duplication between initial setup and session configuration.
    """

    # Filter function to completely block websockets, daily_core, audio logs, specific spam logs,
    # and raw subprocess output (which has its own dedicated passthrough sink in process_pool.py)
    def filter_spam_logs(record):
        # Exclude raw subprocess output - these go through a separate passthrough sink
        if record["extra"].get("subprocess_raw", False):
            return False

        logger_name = record["name"]
        message = record["message"]
        return not (
            logger_name.startswith("websockets")
            or logger_name.startswith("daily_core")
            or logger_name.startswith("openai._base_client")
            or logger_name.startswith("chunk")
            or (logger_name.startswith("logging") and 'TEXT \'{"audio":' in message)
            or (logger_name.startswith("logging") and message.startswith("> BINARY"))
            or (logger_name.startswith("logging") and message.startswith("< BINARY"))
            or (logger_name.startswith("logging") and message.startswith("< TEXT"))
            or (logger_name.startswith("logging") and message.startswith("> TEXT"))
        )

    # Configure logger with patcher BEFORE adding sinks (applies to all environments)
    # Uses module-level log_context_patcher defined above
    logger.configure(patcher=log_context_patcher)

    if ENVIRONMENT == "dev":
        # Development mode format with log context for log isolation
        session_part = (
            "<cyan>[{extra[session_id]}]</cyan> | " if include_session_id else ""
        )
        client_sid_part = (
            "<yellow>[{extra[client_sid]}]</yellow> | " if include_client_sid else ""
        )
        # Log context part - dynamically shows all context fields when available
        # Using a simpler format that shows all fields (empty if not set)
        stdout_fmt = (
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            f"{session_part}"
            f"{client_sid_part}"
            "<magenta>[{extra[_log_context]}]</magenta> | "
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
            filter=filter_spam_logs,  # Also filter spam and subprocess_raw logs in production
            enqueue=True,
            backtrace=False,  # Keep JSON logs concise and predictable
            diagnose=False,  # Prevent sensitive data leakage and performance overhead
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


def configure_session_logger(session_id: str, client_sid: Optional[str] = None):
    """
    Configure the logger to automatically include session_id and client_sid in all log entries.
    This should be called once at the start of a subprocess.
    """
    logger.remove()
    _setup_logger_sinks(include_session_id=True, include_client_sid=bool(client_sid))

    extra_context = {"session_id": session_id}
    if client_sid:
        extra_context["client_sid"] = client_sid

    # Must include patcher to preserve log context injection (configure() replaces all settings)
    logger.configure(patcher=log_context_patcher, extra=extra_context)
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
