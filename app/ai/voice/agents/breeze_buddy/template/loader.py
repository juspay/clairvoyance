"""
Flow Configuration Loader

This module provides functionality to load templates from the database.
"""

from typing import Dict, Optional

from app.ai.voice.agents.breeze_buddy.template.types import (
    TemplateModel,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.template import get_template_by_merchant


class FlowConfigLoader:
    """Loads templates from database"""

    async def _load_template_from_db(
        self,
        merchant_id: str,
        name: str = "order-confirmation",
        shop_identifier: Optional[str] = None,
    ) -> Optional[TemplateModel]:
        """
        Load complete template from database.

        Args:
            merchant_id: Merchant identifier
            name: Template name (defaults to "order-confirmation")
            shop_identifier: Optional shop-specific identifier

        Returns:
            TemplateModel if found, None otherwise
        """
        logger.info(
            f"Loading template for merchant={merchant_id}, name={name}, "
            f"shop={shop_identifier}"
        )

        # Load from database using accessor
        template = await get_template_by_merchant(merchant_id, shop_identifier, name)

        if template:
            nodes_count = len(template.flow.get("nodes", []))
            logger.info(
                f"Successfully loaded template: {template.id} with {nodes_count} nodes"
            )
        else:
            logger.warning(f"No template found for merchant={merchant_id}, name={name}")

        return template

    def render_task_messages(
        self, task_messages: list, variables: Dict[str, str]
    ) -> list:
        """
        Render task messages with runtime variables.

        Args:
            task_messages: List of task message objects
            variables: Dictionary of variable values

        Returns:
            List of rendered task messages
        """
        rendered_messages = []
        for message in task_messages:
            if isinstance(message, dict) and "content" in message:
                content = message["content"]
                # Replace variables in content
                for key, value in variables.items():
                    placeholder = f"{{{key}}}"
                    content = content.replace(placeholder, str(value))

                rendered_message = message.copy()
                rendered_message["content"] = content
                rendered_messages.append(rendered_message)
            else:
                rendered_messages.append(message)

        return rendered_messages

    async def load_template(
        self,
        merchant_id: str,
        template: str,
        template_vars: Dict[str, str],
        shop_identifier: Optional[str] = None,
    ) -> TemplateModel:
        """
        Load template and render task messages with variables.

        Args:
            merchant_id: Merchant identifier
            template: str type
            template_vars: Variables for template rendering
            shop_identifier: Optional shop-specific identifier

        Returns:
            TemplateModel with rendered task messages

        Raises:
            ValueError: If template not found
        """

        # Load template from database
        template_obj = await self._load_template_from_db(
            merchant_id, template, shop_identifier
        )

        if not template_obj:
            raise ValueError(
                f"No template found for merchant={merchant_id}, template={template}"
            )

        # Get nodes from flow structure
        nodes = template_obj.flow.get("nodes", [])

        # Filter only active nodes
        total_nodes = len(nodes)
        active_nodes = [node for node in nodes if node.get("is_active", True)]
        template_obj.flow["nodes"] = active_nodes

        logger.info(
            f"Filtered to {len(active_nodes)} active nodes from {total_nodes} total nodes"
        )

        # Render task messages and role messages with variables for each active node
        for node in template_obj.flow["nodes"]:
            # Get task_messages from node
            task_messages = node.get("task_messages", [])

            # Convert TaskMessage objects to dicts for rendering
            task_message_dicts = []
            for msg in task_messages:
                if hasattr(msg, "model_dump"):
                    task_message_dicts.append(msg.model_dump())
                elif isinstance(msg, dict):
                    task_message_dicts.append(msg)
                else:
                    # Fallback for other formats
                    task_message_dicts.append(
                        {"role": getattr(msg, "role", "system"), "content": str(msg)}
                    )

            rendered_dicts = self.render_task_messages(
                task_message_dicts, template_vars
            )

            # Update node with rendered task messages
            node["task_messages"] = rendered_dicts

            # Get role_messages from node
            role_messages = node.get("role_messages", [])

            # Convert role messages to dicts for rendering
            role_message_dicts = []
            for msg in role_messages:
                if hasattr(msg, "model_dump"):
                    role_message_dicts.append(msg.model_dump())
                elif isinstance(msg, dict):
                    role_message_dicts.append(msg)
                else:
                    # Fallback for other formats
                    role_message_dicts.append(
                        {"role": getattr(msg, "role", "system"), "content": str(msg)}
                    )

            rendered_role_dicts = self.render_task_messages(
                role_message_dicts, template_vars
            )

            # Update node with rendered role messages
            node["role_messages"] = rendered_role_dicts

        logger.info(f"Rendered task messages for template {template_obj.name}")
        return template_obj
