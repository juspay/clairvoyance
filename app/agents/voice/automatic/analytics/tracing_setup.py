# tracing_setup.py
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from typing import Dict, Any, Optional
import time

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

def create_tool_span(function_name: str, arguments: Dict[str, Any], tool_call_id: str):
    """Create a new span for tool execution"""
    if not ENABLE_TRACING:
        return None
        
    tracer = trace.get_tracer(__name__)
    span = tracer.start_span(
        name=f"tool.{function_name}",
        attributes={
            "tool.name": function_name,
            "tool.call_id": tool_call_id,
            "tool.arguments": str(arguments),
            "span.kind": "internal"
        }
    )
    return span

def complete_tool_span(span, result: str, success: bool = True, execution_time_ms: Optional[float] = None):
    """Complete a tool span with result and metrics"""
    if not span or not ENABLE_TRACING:
        return
        
    try:
        # Set span status
        if success:
            span.set_status(Status(StatusCode.OK))
        else:
            span.set_status(Status(StatusCode.ERROR, "Tool execution failed"))
        
        # Add result and timing attributes
        span.set_attribute("tool.result", result[:1000])  # Truncate long results
        span.set_attribute("tool.success", success)
        
        if execution_time_ms is not None:
            span.set_attribute("tool.execution_time_ms", execution_time_ms)
        
        # Add result length for analysis
        span.set_attribute("tool.result_length", len(result))
        
    except Exception as e:
        logger.error(f"Error setting span attributes: {e}")
    finally:
        span.end()

def create_assistant_launch_span(session_id: str, mode: str, user_name: str = None):
    """Create a new span for assistant launch tracking"""
    if not ENABLE_TRACING:
        return None
        
    tracer = trace.get_tracer(__name__)
    span = tracer.start_span(
        name="assistant.launch",
        attributes={
            "assistant.session_id": session_id,
            "assistant.mode": mode,
            "assistant.user_name": user_name or "guest",
            "span.kind": "internal",
            "assistant.component": "voice_agent"
        }
    )
    return span

def complete_assistant_launch_span(span, success: bool = True, execution_time_ms: Optional[float] = None, 
                                 error_message: str = None, components_initialized: int = None):
    """Complete an assistant launch span with metrics"""
    if not span or not ENABLE_TRACING:
        return
        
    try:
        # Set span status
        if success:
            span.set_status(Status(StatusCode.OK))
        else:
            span.set_status(Status(StatusCode.ERROR, error_message or "Assistant launch failed"))
        
        # Add launch metrics
        span.set_attribute("assistant.launch_success", success)
        
        if execution_time_ms is not None:
            span.set_attribute("assistant.launch_time_ms", execution_time_ms)
        
        if error_message:
            span.set_attribute("assistant.error_message", error_message)
            
        if components_initialized is not None:
            span.set_attribute("assistant.components_initialized", components_initialized)
        
    except Exception as e:
        logger.error(f"Error setting assistant launch span attributes: {e}")
    finally:
        span.end()
