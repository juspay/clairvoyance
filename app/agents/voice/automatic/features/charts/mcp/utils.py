from typing import Any, Dict

from app.agents.voice.automatic.features.charts.chart_tools import (
    _register_pending_chart_emission,
)
from app.core.logger import logger


async def _store_ui_components_from_mcp(
    self, ui_components: list[Dict[str, Any]]
) -> None:
    """Store UI components from MCP response in local registry for LLMSpyProcessor pickup."""
    try:

        session_id = self._session_context.session_id

        for ui_component in ui_components:
            # Transform MCP UI component format to expected format
            component_data = ui_component.copy()

            # Map MCP fields to expected format
            if "id" in component_data and "componentId" not in component_data:
                component_data["componentId"] = component_data["id"]

            _register_pending_chart_emission(session_id, component_data)
            component_id = component_data.get(
                "componentId", component_data.get("id", "unknown")
            )
            logger.info(f"[{session_id}] Stored UI component from MCP: {component_id}")

    except Exception as e:
        logger.error(
            f"Tool Error: [mcp_ui_components] Error storing UI components from MCP: {e}"
        )
