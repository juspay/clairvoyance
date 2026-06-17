# tracing_setup.py
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.core.config.static import (
    AUTOMATIC_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT,
    AUTOMATIC_OTEL_EXPORTER_OTLP_TRACES_HEADERS,
    ENABLE_TRACING,
)
from app.core.logger import logger

# Module-level idempotency guard to prevent multiple tracing initializations
_tracing_initialized = False


def setup_tracing(service_name: str):
    global _tracing_initialized

    if _tracing_initialized:
        logger.debug("Tracing already initialized. Skipping setup.")
        return

    if not ENABLE_TRACING:
        logger.info("Tracing is disabled. Skipping setup.")
        return

    resource = Resource(attributes={SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)

    if (
        AUTOMATIC_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
        and AUTOMATIC_OTEL_EXPORTER_OTLP_TRACES_HEADERS
    ):
        headers_value = (
            AUTOMATIC_OTEL_EXPORTER_OTLP_TRACES_HEADERS.replace("Authorization=", "")
            if AUTOMATIC_OTEL_EXPORTER_OTLP_TRACES_HEADERS.startswith("Authorization=")
            else AUTOMATIC_OTEL_EXPORTER_OTLP_TRACES_HEADERS
        )

        exporter = OTLPSpanExporter(
            endpoint=AUTOMATIC_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT,
            headers={"Authorization": headers_value},
        )
        logger.info(
            f"Automatic agent tracing configured for endpoint: {AUTOMATIC_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT}"
        )
    else:
        exporter = OTLPSpanExporter()
        logger.warning(
            "Automatic agent OTLP env vars not found, using default OTEL configuration"
        )

    processor = BatchSpanProcessor(exporter)
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    _tracing_initialized = True

    logger.info(f"Tracing successfully set up for service: {service_name}")
