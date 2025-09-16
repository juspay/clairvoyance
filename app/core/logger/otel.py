from loguru import logger

# OTEL imports with availability check
try:
    from opentelemetry._logs import get_logger_provider, set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    from app.core.config import (
        ENABLE_OTEL_LOGGING,
        OTEL_COLLECTOR_LOGS_ENDPOINT,
        OTEL_SERVICE_NAME,
    )

    OTEL_AVAILABLE = True
except ImportError as e:
    OTEL_AVAILABLE = False
    logger.warning(f"OTEL logging components not available: {e}")

# Global OTEL components
_otel_logger_provider = None
_otel_logger = None
_otel_handler = None


def get_otel_logger():
    """Get or create the global OTEL logger (singleton pattern)"""
    global _otel_logger_provider, _otel_logger, _otel_handler

    # Return existing logger if already initialized
    if _otel_logger is not None:
        return _otel_logger

    if _otel_logger is None and ENABLE_OTEL_LOGGING and OTEL_AVAILABLE:
        try:
            # Create resource with proper attribute format
            resource = Resource(attributes={"service.name": OTEL_SERVICE_NAME})

            # Create logger provider (only if not already set)
            try:
                _otel_logger_provider = LoggerProvider(resource=resource)
                set_logger_provider(_otel_logger_provider)
            except Exception as provider_error:
                # If provider already exists, get the existing one
                _otel_logger_provider = get_logger_provider()
                logger.info(f"Using existing OTEL logger provider: {provider_error}")

            # Create exporter and processor with improved configuration
            log_exporter = OTLPLogExporter(
                endpoint=f"{OTEL_COLLECTOR_LOGS_ENDPOINT}/v1/logs",
                headers={},
                timeout=30,
            )
            log_processor = BatchLogRecordProcessor(
                log_exporter,
                max_export_batch_size=100,
                export_timeout_millis=30000,
                schedule_delay_millis=5000,
            )
            _otel_logger_provider.add_log_record_processor(log_processor)

            # Create LoggingHandler for integration with standard logging
            _otel_handler = LoggingHandler(logger_provider=_otel_logger_provider)

            # Get logger instance (for direct emission if needed)
            _otel_logger = _otel_logger_provider.get_logger(__name__)

            logger.info("OTEL logger initialized")
        except Exception as e:
            logger.error(f"Failed to initialize OTEL logger: {e}")
            # Add more detailed error info
            import traceback

            logger.error(f"OTEL initialization traceback: {traceback.format_exc()}")
            # Disable OTEL for this session to prevent repeated errors
            globals()["ENABLE_OTEL_LOGGING"] = False

    return _otel_logger


def get_otel_handler():
    """Get the OTEL handler for use in otel_sink"""
    global _otel_handler
    # Ensure logger is initialized first
    get_otel_logger()
    return _otel_handler
