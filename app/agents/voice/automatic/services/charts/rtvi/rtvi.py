from pipecat.processors.frameworks.rtvi import RTVIProcessor, RTVIServerMessageFrame

from app.core.logger import logger
from app.agents.voice.automatic.services.charts.chart_tools import get_pending_chart_emissions

async def emit_chart_components(rtvi: RTVIProcessor, function_name: str, session_id: str) -> None:
    """Emit chart components via RTVI frames after function calls."""
    del function_name  # Unused parameter
    try:            
        pending_charts = get_pending_chart_emissions(session_id)

        for chart_data in pending_charts:
            await rtvi.push_frame(
                RTVIServerMessageFrame(
                    data={
                        "type": "ui-component",
                        "payload": chart_data
                    }
                )
            )
            
    except Exception as e:
        logger.error(f"Error emitting chart components for session {session_id}: {e}")