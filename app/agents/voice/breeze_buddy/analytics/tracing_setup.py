import json
from functools import wraps

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pipecat.utils.tracing.turn_context_provider import get_current_turn_context

from app.core.config import (
    BUDDY_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT,
    BUDDY_OTEL_EXPORTER_OTLP_TRACES_HEADERS,
    ENABLE_BREEZE_BUDDY_TRACING,
)
from app.core.logger import logger


def setup_tracing(service_name: str):
    if not ENABLE_BREEZE_BUDDY_TRACING:
        logger.info("Tracing is disabled. Skipping setup.")
        return

    resource = Resource(attributes={SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)

    if (
        BUDDY_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
        and BUDDY_OTEL_EXPORTER_OTLP_TRACES_HEADERS
    ):
        headers_value = (
            BUDDY_OTEL_EXPORTER_OTLP_TRACES_HEADERS.replace("Authorization=", "")
            if BUDDY_OTEL_EXPORTER_OTLP_TRACES_HEADERS.startswith("Authorization=")
            else BUDDY_OTEL_EXPORTER_OTLP_TRACES_HEADERS
        )

        exporter = OTLPSpanExporter(
            endpoint=BUDDY_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT,
            headers={"Authorization": headers_value},
        )
        logger.info(
            f"Breeze Buddy tracing configured for project: {BUDDY_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT}"
        )
    else:
        exporter = OTLPSpanExporter()
        logger.warning(
            "Buddy-specific env vars not found, using default OTEL configuration"
        )

    processor = BatchSpanProcessor(exporter)
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    logger.info(f"Tracing successfully set up for service: {service_name}")


def auto_trace(tool_name: str):
    """
    Decorator to automatically trace tool calls with turn context nesting.

    Args:
        tool_name: The name of the tool being traced

    Returns:
        Decorated function that creates proper tracing spans
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            if not ENABLE_BREEZE_BUDDY_TRACING:
                logger.debug(f"Tracing disabled, executing {tool_name} without tracing")
                return await func(*args, **kwargs)

            tracer = trace.get_tracer(__name__)
            turn_context = get_current_turn_context()

            # Create span with turn context for proper nesting
            span_name = f"Tool: {tool_name}"
            with tracer.start_span(
                span_name,
                kind=trace.SpanKind.CLIENT,
                context=turn_context,
            ) as span:
                span.set_attribute("tool.name", tool_name)
                span.set_attribute("tool.service", "breeze-buddy")

                # Add function arguments as attributes if present
                if kwargs:
                    span.set_attribute("tool.args", json.dumps(kwargs))

                logger.info(f"Auto-tracing tool call: {tool_name}")

                try:
                    result = await func(*args, **kwargs)
                    span.set_attribute("tool.status", "success")
                    return result
                except Exception as e:
                    span.set_attribute("tool.status", "error")
                    span.set_attribute("tool.error", str(e))
                    logger.error(f"Error in {tool_name}: {e}")
                    raise

        return wrapper

    return decorator
