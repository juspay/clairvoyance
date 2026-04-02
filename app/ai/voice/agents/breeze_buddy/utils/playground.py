"""Playground utilities for Breeze Buddy."""

from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    TemplateModel,
)
from app.core.logger import logger


def apply_playground_config_overrides(lead, template: TemplateModel) -> None:
    """Apply playground configuration overrides from lead metaData onto template.

    Mutates template.configurations in-place. Shared by:
    - managers/calls.py: before greeting synthesis (pre-WebSocket)
    - agent/flow.py: at WebSocket connect time (load_template_config)
    """
    if not (lead.metaData and lead.metaData.get("playground")):
        return
    configurations_dict = lead.metaData.get("configurations")
    if not (configurations_dict and isinstance(configurations_dict, dict)):
        return
    try:
        template.configurations = ConfigurationModel(**configurations_dict)
        logger.info(f"Applied playground configuration overrides for lead {lead.id}")
    except Exception:
        rejected_fields = list(configurations_dict.keys())
        logger.warning(
            f"Failed to parse playground configurations for lead {lead.id}; "
            f"rejected fields: {rejected_fields}"
        )
