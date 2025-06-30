# tracing_setup.py
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry import trace

from loguru import logger

from app.core.config import ENABLE_TRACING

def setup_tracing(service_name: str):
    if not ENABLE_TRACING:
        logger.info("Tracing is disabled. Skipping setup.")
        return

    resource = Resource(attributes={SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)

    exporter = OTLPSpanExporter()
    logger.debug(f"Exporter initialized with endpoint: {exporter._endpoint}")

    processor = BatchSpanProcessor(exporter)
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)