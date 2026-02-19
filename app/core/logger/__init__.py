import json
import logging
import sys
from contextvars import ContextVar
from typing import Optional

from loguru import logger

# Remove the default sink to have full control over logging.
logger.remove()

# Use environment variables directly to avoid circular import
from app.core.config.static import ENVIRONMENT, PROD_LOG_LEVEL

# ---------------------------------------------------------------------------
# Generic per-async-task log context
#
# A single ContextVar holds an arbitrary dict of key-value pairs. Set it once
# at the entry point of any async task (agent, background job, request handler)
# and every log in that task — including DB accessors, template handlers,
# webhooks, and third-party library logs intercepted by Loguru — automatically
# carries those fields without any changes to downstream code.
#
# Concurrency safe: each asyncio Task owns its own copy of ContextVar state.
# Setting context in one task has zero effect on any other concurrent task.
# ---------------------------------------------------------------------------
_log_context: ContextVar[dict] = ContextVar("log_context", default={})


def set_log_context(**kwargs: Optional[str]) -> None:
    """
    Set arbitrary key-value pairs as log context for the current async task.

    Every subsequent log call anywhere in the same async task — including DB
    accessors, template handlers, webhook senders, and third-party library
    logs intercepted by Loguru — will automatically carry these fields.

    Callers decide which fields are relevant; core logger has no opinion on
    field names. Examples of what callers might pass:

        # Breeze Buddy agent after lead is resolved:
        set_log_context(
            lead_id=self.lead.id,
            merchant_id=self.lead.merchant_id,
            template=self.lead.template,
            phone_number=(self.lead.payload or {}).get("customer_mobile_number", ""),
            call_sid=self.call_sid or "",
        )

        # Automatic agent subprocess after session is established:
        set_log_context(session_id=args.session_id, client_sid=args.client_sid)

        # Any future agent or background worker with its own relevant fields:
        set_log_context(job_id=job.id, queue=job.queue_name)
    """
    _log_context.set(kwargs)


def clear_log_context() -> None:
    """
    Clear the log context for the current async task.

    Optional — asyncio Tasks discard their ContextVar state automatically
    when they complete, so explicit clearing is only needed if you reuse a
    long-lived coroutine across multiple logical operations.
    """
    _log_context.set({})


def _log_context_patcher(record: dict) -> None:  # type: ignore[type-arg]
    """
    Loguru patcher: injects the current async-task's log context into every
    log record's extra dict before it reaches any sink.

    Only non-empty string values are added, so logs that fire outside of a
    task context (e.g. startup, scheduler ticks) are not polluted with empty
    fields.
    """
    ctx = _log_context.get()
    if ctx:
        extra = record["extra"]
        for key, value in ctx.items():
            if value:
                extra[key] = value


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
        frame = logging.currentframe()
        depth = 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            next_frame = frame.f_back
            if next_frame is None:
                break
            frame = next_frame
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

    logger.configure(extra=extra_context, patcher=_log_context_patcher)  # type: ignore[arg-type]
    # Also set up logging interception for session-based logging
    setup_logging_interception()


def get_bound_logger(**context):
    """
    Return a Loguru bound logger enriched with the provided context fields.

    All keyword arguments are added as structured extra fields on every log
    entry emitted through the returned logger. Use this whenever you need
    per-call / per-request log context without polluting the process-global
    logger state.

    Safe for concurrent async usage in the same process: logger.bind() returns
    a new logger instance and does NOT mutate any global state.

    Examples:
        # In an Agent class after the lead is resolved:
        self.logger = get_bound_logger(
            lead_id=self.lead.id,
            merchant_id=self.lead.merchant_id,
            template=self.lead.template,
            phone_number=(self.lead.payload or {}).get("customer_mobile_number"),
            call_sid=self.call_sid,
        )

        # In a background task loop:
        lead_logger = get_bound_logger(
            lead_id=locked_lead.id,
            merchant_id=locked_lead.merchant_id,
            template=locked_lead.template,
        )
    """
    return logger.bind(**context)


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


# Initial logger configuration — register the patcher so log context flows
# into every log record from the very first import.
_setup_logger_sinks(include_session_id=False)
logger.configure(patcher=_log_context_patcher)  # type: ignore[arg-type]

# Set up logging interception for unified logging
setup_logging_interception()

# Export the configured logger for use throughout the application.
__all__ = [
    "logger",
    "configure_session_logger",
    "get_bound_logger",
    "set_log_context",
    "clear_log_context",
]
