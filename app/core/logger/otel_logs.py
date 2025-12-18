"""
OTEL Log Export Configuration for Crane.

This module sets up OpenTelemetry log export to send application logs to Crane
(OpenObserve). It integrates with Loguru via the otel_sink in __init__.py.

Usage:
    from app.core.logger.otel_logs import setup_otel_logging
    setup_otel_logging("clairvoyance")  # Call once in main process (run.py)
"""

from opentelemetry._logs import SeverityNumber

from app.core.config.static import ENABLE_OTEL_LOGS, OTEL_EXPORTER_OTLP_LOGS_ENDPOINT

# Module-level flag to prevent duplicate initialization (for pool mode)
_otel_logging_initialized = False

# Map Loguru level names to OTEL SeverityNumber
LEVEL_TO_SEVERITY = {
    "TRACE": SeverityNumber.TRACE,
    "DEBUG": SeverityNumber.DEBUG,
    "INFO": SeverityNumber.INFO,
    "SUCCESS": SeverityNumber.INFO,  # Loguru's SUCCESS maps to INFO
    "WARNING": SeverityNumber.WARN,
    "ERROR": SeverityNumber.ERROR,
    "CRITICAL": SeverityNumber.FATAL,
}


def setup_otel_logging(service_name: str) -> None:
    """
    Initialize OTEL log export to Crane.

    This function:
    1. Checks if OTEL logging is enabled via environment variables
    2. Sets up the OTEL LoggerProvider with BatchLogRecordProcessor
    3. Configures OTLPLogExporter to send logs to the configured endpoint
    4. Adds the otel_sink to Loguru to capture all logs

    Args:
        service_name: The service name to use in OTEL resource attributes

    Note:
        This should be called once per process. In pool mode, the same
        subprocess handles multiple sessions, so the idempotency guard
        prevents duplicate Loguru sinks.
    """
    global _otel_logging_initialized
    if _otel_logging_initialized:
        return  # Already initialized in this process

    import logging

    from loguru import logger

    # Filter to block only Crane-related logs, not all urllib3/opentelemetry logs
    class CraneLogFilter(logging.Filter):
        def filter(self, record):
            message = record.getMessage()
            # Block only logs about Crane endpoint
            if "crane.beta.breeze.in" in message or "/v1/logs" in message:
                return False
            return True

    # Add filter to urllib3 and opentelemetry loggers
    logging.getLogger("urllib3").addFilter(CraneLogFilter())
    logging.getLogger("opentelemetry").addFilter(CraneLogFilter())

    if not ENABLE_OTEL_LOGS:
        return

    if not OTEL_EXPORTER_OTLP_LOGS_ENDPOINT:
        logger.warning(
            "OTEL logging enabled but OTEL_EXPORTER_OTLP_LOGS_ENDPOINT is not set"
        )
        return

    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource

        # Create resource with service name
        resource = Resource.create(
            {
                "service.name": service_name,
            }
        )

        # Create logger provider
        logger_provider = LoggerProvider(resource=resource)

        # Create OTLP exporter
        otlp_exporter = OTLPLogExporter(
            endpoint=OTEL_EXPORTER_OTLP_LOGS_ENDPOINT,
        )

        # Add batch processor to logger provider
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(otlp_exporter)
        )

        # Set global logger provider
        set_logger_provider(logger_provider)

        # Set the global OTEL logger provider in __init__.py so otel_sink can use it
        from app.core.logger import set_otel_logger_provider

        set_otel_logger_provider(logger_provider)

        # Add OTEL sink to Loguru
        from app.core.logger import otel_sink

        logger.add(
            otel_sink,
            level="DEBUG",  # Capture all levels, filtering happens in the sink
            enqueue=True,  # Thread-safe async logging
            backtrace=False,
            diagnose=False,
        )

        _otel_logging_initialized = True
        logger.info(f"OTEL logging initialized successfully for service: {service_name}, endpoint: {OTEL_EXPORTER_OTLP_LOGS_ENDPOINT}")

    except ImportError as e:
        logger.error(f"Failed to import OTEL dependencies: {e}")
    except Exception as e:
        logger.error(f"Failed to initialize OTEL logging: {e}")


def get_otel_logger_provider():
    """
    Get the global OTEL logger provider instance.

    Returns:
        The OTEL logger provider instance, or None if OTEL logging is not initialized.
    """
    from app.core.logger import _otel_logger_provider

    return _otel_logger_provider
