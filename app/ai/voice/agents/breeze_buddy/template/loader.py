"""
Flow Configuration Loader

This module provides functionality to load templates from the database.
"""

from typing import Dict, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.template.transformation_function import (
    TEMPLATE_FUNCTION_REGISTRY,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    TemplateModel,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.credentials import (
    get_credentials_as_template_vars,
)
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
        shop_identifier: Optional[str] = None,
        call_payload: Optional[Dict[str, str]] = None,
    ) -> Tuple[TemplateModel, Dict[str, str]]:
        """
        Load template and render task messages with variables.

        Args:
            merchant_id: Merchant identifier
            template: str type
            template_vars: Variables for template rendering
            shop_identifier: Optional shop-specific identifier

        Returns:
            TemplateModel with rendered task messages, and dictionary of template variables

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

        template_vars = {}

        # 1. Load credentials from credentials table (global + merchant-specific)
        try:
            credential_vars = await get_credentials_as_template_vars(merchant_id)
            if credential_vars:
                template_vars.update(credential_vars)
                logger.info(
                    f"Loaded {len(credential_vars)} credential vars for merchant {merchant_id}"
                )
        except Exception as e:
            logger.warning(
                f"Failed to load credentials for merchant {merchant_id}: {e}"
            )
        logger.info(
            f"Loaded {len(template_vars)} template vars for merchant {merchant_id}"
        )

        # 2. Load template.secrets (overrides credentials for same keys)
        if template_obj.secrets:
            template_vars.update(template_obj.secrets)
            logger.info(f"Loaded {len(template_obj.secrets)} secrets from template")
        # 3. Load payload fields (overrides both credentials and secrets)
        expected_schema = template_obj.expected_payload_schema or {}
        for field_name, field_schema in expected_schema.items():
            value = None
            if call_payload and field_name in call_payload:
                value = call_payload[field_name]
            else:
                logger.warning(f"Field '{field_name}' from schema not found in payload")
                value = ""
            # Check for function in schema
            function_name = None
            if isinstance(field_schema, dict):
                function_name = field_schema.get("function")
            if function_name and function_name in TEMPLATE_FUNCTION_REGISTRY:
                try:
                    func = TEMPLATE_FUNCTION_REGISTRY[function_name]
                    if value is None or value == "":
                        value = func()
                    else:
                        value = func(value)
                except Exception as e:
                    logger.warning(
                        f"Error applying function '{function_name}' to field '{field_name}': {e}"
                    )
            template_vars[field_name] = value

        logger.info(
            f"Dynamically built template_vars from schema: {list(template_vars.keys())}"
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
        return template_obj, template_vars
