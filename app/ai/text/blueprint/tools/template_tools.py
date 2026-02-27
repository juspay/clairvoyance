"""
Custom LangChain tools for template awareness in the Blueprint agent.

These tools wrap the existing database accessors so the Template Architect
subagent can discover and reference production templates during generation.

Tools:
    list_templates_tool  — Returns lightweight metadata for all templates.
    get_template_by_id_tool — Fetches the full template JSON by UUID.
"""

import json
from typing import Optional

from langchain_core.tools import tool  # type: ignore[import-not-found]

from app.core.logger import logger


@tool
async def list_templates_tool(
    merchant_id: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> str:
    """List all available production templates (metadata only).

    Returns template names, IDs, merchant info, and active status.
    Use this to discover existing templates the user might be referencing.

    Args:
        merchant_id: Optional merchant ID to filter by.
        is_active: Optional filter for active/inactive templates.

    Returns:
        JSON string with list of template metadata objects.
    """
    from app.database.accessor.breeze_buddy.template import get_templates_list

    filters: dict = {}
    if merchant_id:
        filters["merchant_id"] = merchant_id
    if is_active is not None:
        filters["is_active"] = is_active

    try:
        templates = await get_templates_list(filters)
        result = [
            {
                "id": t.id,
                "name": t.name,
                "merchant_id": t.merchant_id,
                "shop_identifier": t.shop_identifier,
                "is_active": t.is_active,
            }
            for t in templates
        ]
        logger.info(f"list_templates_tool returned {len(result)} templates")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"list_templates_tool failed: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


@tool
async def get_template_by_id_tool(template_id: str) -> str:
    """Fetch a complete production template by its UUID.

    Returns the full template JSON including the flow structure, nodes,
    functions, hooks, and configurations. Use this after discovering a
    template ID from list_templates_tool.

    Args:
        template_id: The UUID of the template to fetch.

    Returns:
        JSON string with the full template, or an error message.
    """
    from app.database.accessor.breeze_buddy.template import get_template_by_id

    try:
        template = await get_template_by_id(template_id)
        if template is None:
            return json.dumps({"error": f"Template '{template_id}' not found"})

        result = template.model_dump(mode="json")
        logger.info(f"get_template_by_id_tool returned template: {template.id}")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"get_template_by_id_tool failed: {e}", exc_info=True)
        return json.dumps({"error": str(e)})
