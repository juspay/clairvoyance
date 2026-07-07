"""Playground utilities for Breeze Buddy."""

import copy
from typing import Dict, Optional

from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.field_resolver import (
    replace_placeholders,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    TemplateModel,
)
from app.core.logger import logger


def apply_playground_overrides(
    lead, template: TemplateModel, template_vars: Optional[Dict[str, str]] = None
) -> TemplateModel:
    """Apply playground configuration and flow overrides from lead metaData onto template.

    Creates a copy of the template to avoid mutating shared/cached instances.
    Shared by:
    - managers/calls.py: before greeting synthesis (pre-WebSocket)
    - agent/flow.py: at WebSocket connect time (load_template_config)

    Returns:
        The original template if no playground mode, or a copy with overrides applied.
    """
    if not (lead.metaData and lead.metaData.get("playground")):
        return template

    # Create a copy to avoid mutating shared/cached template instances
    template = template.model_copy()
    configurations_dict = lead.metaData.get("configurations")
    if configurations_dict and isinstance(configurations_dict, dict):
        try:
            template.configurations = ConfigurationModel(**configurations_dict)
            logger.info(
                f"Applied playground configuration overrides for lead {lead.id}"
            )
        except Exception:
            rejected_fields = list(configurations_dict.keys())
            logger.warning(
                f"Failed to parse playground configurations for lead {lead.id}; "
                f"rejected fields: {rejected_fields}"
            )
            return template

    # Apply flow override (deep copy to avoid mutating lead.metaData during substitution)
    flow_override = copy.deepcopy(lead.metaData.get("flow_override"))
    logger.info(f"Flow override from metaData: {flow_override is not None}")
    if flow_override and isinstance(flow_override, dict):
        try:
            runtime_data = template.flow.get("_runtime_data")
            if template_vars:
                for node in flow_override.get("nodes", []):
                    # Re-render task_messages
                    for msg in node.get("task_messages", []):
                        if isinstance(msg, dict) and "content" in msg:
                            msg["content"] = replace_placeholders(
                                msg["content"], template_vars
                            )
                    # Re-render role_messages
                    for msg in node.get("role_messages", []):
                        if isinstance(msg, dict) and "content" in msg:
                            msg["content"] = replace_placeholders(
                                msg["content"], template_vars
                            )
            if runtime_data is not None:
                flow_override["_runtime_data"] = runtime_data
            template.flow = flow_override
            logger.info(f"Applied playground flow override for lead {lead.id}")

        except Exception as e:
            logger.warning(
                f"Failed to apply playground flow override for lead {lead.id}: {e}"
            )
    else:
        logger.info(f"No flow override found in metaData for lead {lead.id}")

    return template
