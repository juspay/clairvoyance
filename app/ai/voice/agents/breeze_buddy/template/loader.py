"""
Flow Configuration Loader

This module provides functionality to load templates from the database.
"""

import asyncio
from typing import Dict, List, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.template.transformation_function import (
    TEMPLATE_FUNCTION_REGISTRY,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    DataSourceRef,
    FlowMode,
    TemplateModel,
)
from app.ai.voice.agents.breeze_buddy.template.utils import render_messages_with_vars
from app.core.logger import logger
from app.database.accessor.breeze_buddy.credentials import (
    get_credentials_as_template_vars,
)
from app.database.accessor.breeze_buddy.data_source import get_data_source_by_id
from app.database.accessor.breeze_buddy.template import (
    get_template_by_id_with_fallback,
)
from app.services.data_sources import (
    DATA_SOURCE_UNAVAILABLE,
    data_source_in_template_scope,
)
from app.services.google.sheets import fetch_formatted
from app.services.redis import get_redis_service


class FlowConfigLoader:
    """Loads templates from database"""

    async def _load_template_from_db(
        self,
        reseller_id: str,
        name: str = "order-confirmation",
        merchant_id: Optional[str] = None,
        template_id: Optional[str] = None,
    ) -> Optional[TemplateModel]:
        """
        Load complete template from database.

        Args:
            reseller_id: Reseller identifier
            name: Template name (defaults to "order-confirmation")
            merchant_id: Optional merchant-specific identifier
            template_id: Optional template UUID (preferred over name-based lookup)

        Returns:
            TemplateModel if found, None otherwise
        """
        template = await get_template_by_id_with_fallback(
            template_id=template_id,
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            name=name,
        )

        if template:
            nodes_count = len(template.flow.get("nodes", []))
            logger.info(
                "Successfully loaded template: %s with %d nodes",
                template.id,
                nodes_count,
            )
        else:
            logger.warning(
                "No template found for reseller=%s, name=%s (template_id=%s)",
                reseller_id,
                name,
                template_id,
            )

        return template

    def _render_direct_mode_flow(self, flow: Dict, variables: Dict[str, str]) -> None:
        """Render {placeholder} variables in a direct-mode flow JSON in place.

        Direct mode only has a top-level ``system_prompt`` to render. Function
        descriptions are not rendered, matching flow-mode behavior.
        """
        system_prompt = flow.get("system_prompt")
        if isinstance(system_prompt, str) and system_prompt:
            for key, value in variables.items():
                system_prompt = system_prompt.replace(f"{{{key}}}", str(value))
            flow["system_prompt"] = system_prompt

    def _render_ivr_flow(self, flow: Dict, variables: Dict[str, str]) -> None:
        """Render {placeholder} variables in an ivr-mode flow JSON in place.

        IVR nodes have no task/role messages. Each node has a spoken ``prompt``
        plus optional ``invalid_prompt`` / ``on_timeout_message``, and each
        option may carry a spoken ``message``. Render all of them with the same
        simple substitution direct mode uses.
        """

        def _sub(text):
            if isinstance(text, str) and text:
                for key, value in variables.items():
                    text = text.replace(f"{{{key}}}", str(value))
            return text

        nodes = flow.get("nodes", {})
        if not isinstance(nodes, dict):
            return
        for node in nodes.values():
            if not isinstance(node, dict):
                continue
            for field in ("prompt", "invalid_prompt", "on_timeout_message"):
                if field in node:
                    node[field] = _sub(node[field])
            for option in node.get("options", []) or []:
                if isinstance(option, dict) and "message" in option:
                    option["message"] = _sub(option["message"])

    def render_task_messages(
        self, task_messages: list, variables: Dict[str, str]
    ) -> list:
        """Method-style alias preserved for the voice loader call sites."""
        return render_messages_with_vars(task_messages, variables)

    async def _fetch_data_source_content(
        self,
        ref: DataSourceRef,
        template_obj: TemplateModel,
    ) -> str:
        """
        Fetch formatted sheet content for a DataSourceRef.

        Priority:
        1. Redis cache (key ``datasource:content:{data_source_id}``, shared across leads)
        2. Live Google Sheets fetch with 800 ms timeout
        3. Fallback: DATA_SOURCE_UNAVAILABLE
        """
        # 1. Redis cache check (shared key across all leads using this data source)
        cache_key = f"datasource:content:{ref.data_source_id}"
        try:
            redis = await get_redis_service()
            cached = await redis.get(cache_key)
            if cached:
                logger.info(
                    "Data source cache hit: ds=%s name=%s", ref.data_source_id, ref.name
                )
                return cached
        except Exception as exc:
            logger.warning(
                "Redis cache check failed for datasource '%s': %s", ref.name, exc
            )

        # 2. Live fetch
        try:
            ds = await get_data_source_by_id(ref.data_source_id)
            if not ds:
                logger.warning(
                    "Data source %s not found in DB (ref name=%s)",
                    ref.data_source_id,
                    ref.name,
                )
                return DATA_SOURCE_UNAVAILABLE
            if not data_source_in_template_scope(
                ds, template_obj.reseller_id, template_obj.merchant_id
            ):
                logger.warning(
                    "Data source %s is inactive or outside template scope "
                    "(template=%s, ref name=%s)",
                    ref.data_source_id,
                    template_obj.id,
                    ref.name,
                )
                return DATA_SOURCE_UNAVAILABLE

            content = await asyncio.wait_for(
                fetch_formatted(
                    spreadsheet_id=ds.spreadsheet_id,
                    sheet_name=ds.sheet_name,
                    columns=ds.columns,
                    format=ds.format,
                ),
                timeout=0.8,
            )
            logger.info(
                "Fetched data source '%s' live (%d chars)", ref.name, len(content)
            )
            return content
        except asyncio.TimeoutError:
            logger.warning(
                "Timeout fetching data source '%s' (>800 ms); using fallback", ref.name
            )
            return DATA_SOURCE_UNAVAILABLE
        except Exception as exc:
            logger.error(
                "Error fetching data source '%s': %s", ref.name, exc, exc_info=True
            )
            return DATA_SOURCE_UNAVAILABLE

    async def load_template(
        self,
        reseller_id: str,
        template: str,
        merchant_id: Optional[str] = None,
        call_payload: Optional[Dict[str, str]] = None,
        template_id: Optional[str] = None,
    ) -> Tuple[TemplateModel, Dict[str, str], List[Dict]]:
        """
        Load template and render task messages with variables.

        Args:
            reseller_id: Reseller identifier
            template: Template name (used as fallback if template_id not provided)
            merchant_id: Optional merchant-specific identifier
            call_payload: Optional payload variables
            template_id: Optional template UUID (preferred over name-based lookup)

        Returns:
            Tuple of (template, template_vars, data_source_messages)
        """

        # Load template from database
        template_obj = await self._load_template_from_db(
            reseller_id, template, merchant_id, template_id=template_id
        )

        if not template_obj:
            raise ValueError(
                f"No template found for reseller={reseller_id}, template={template}"
            )

        template_vars = {}

        # 1. Load credentials from credentials table (global + merchant-specific)
        try:
            credential_vars = await get_credentials_as_template_vars(reseller_id)
            if credential_vars:
                template_vars.update(credential_vars)
                logger.info(
                    f"Loaded {len(credential_vars)} credential vars for reseller {reseller_id}"
                )
        except Exception as e:
            logger.warning(
                f"Failed to load credentials for merchant {reseller_id}: {e}"
            )
        logger.info(
            f"Loaded {len(template_vars)} template vars for merchant {reseller_id}"
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
            # Check for function(s) in schema — supports a single string
            # or a list of function names applied sequentially as a pipeline.
            function_names = None
            fn_params = {}
            if isinstance(field_schema, dict):
                raw = field_schema.get("function")
                if isinstance(raw, list):
                    function_names = raw
                elif isinstance(raw, str):
                    function_names = [raw]
                raw_params = field_schema.get("params")
                if isinstance(raw_params, dict):
                    fn_params = raw_params
            if function_names:
                for fn_name in function_names:
                    if fn_name not in TEMPLATE_FUNCTION_REGISTRY:
                        logger.warning(
                            f"Unknown transformation function '{fn_name}' "
                            f"for field '{field_name}', skipping"
                        )
                        continue
                    try:
                        func = TEMPLATE_FUNCTION_REGISTRY[fn_name]
                        if value is None or value == "":
                            value = func(**fn_params) if fn_params else func()
                        else:
                            value = (
                                func(value, **fn_params) if fn_params else func(value)
                            )
                    except Exception as e:
                        logger.warning(
                            f"Error applying function '{fn_name}' to field '{field_name}': {e}"
                        )
            template_vars[field_name] = value

        logger.info(
            f"Dynamically built template_vars from schema: {list(template_vars.keys())}"
        )

        # 4. Inject any remaining payload fields not already in template_vars.
        # This allows payload fields used for infrastructure (e.g. shop_url for
        # MCP server URL resolution) to be available without being declared in
        # expected_payload_schema. Must run before the direct-mode early return
        # so that MCP URL/auth placeholders resolve correctly in both modes.
        if call_payload:
            for field_name, field_value in call_payload.items():
                if field_name not in template_vars:
                    template_vars[field_name] = field_value
                    logger.info(
                        f"Injected extra payload field '{field_name}' into template_vars"
                    )

        # 5. Inject data_source content.
        # "var" sources → template_vars[ref.name] = content (rendered as {name} placeholder)
        # "message" sources → added to _ds_messages list (returned separately, consumed
        #   later in prepare_initial_node / build_flow_config).
        _ds_messages: List[Dict] = []
        if template_obj.data_sources:
            contents = await asyncio.gather(
                *[
                    self._fetch_data_source_content(ref, template_obj)
                    for ref in template_obj.data_sources
                ],
                return_exceptions=True,
            )
            for ref, result in zip(template_obj.data_sources, contents):
                content = (
                    DATA_SOURCE_UNAVAILABLE if isinstance(result, Exception) else result
                )
                if ref.inject_as == "message":
                    _ds_messages.append({"role": "system", "content": content})
                    logger.info("Data source '%s' queued as system message", ref.name)
                else:
                    template_vars[ref.name] = content
                    logger.info(
                        "Data source '%s' injected into template_vars", ref.name
                    )

        # Direct mode has a flat structure (system_prompt + flat function list)
        # rather than a nodes array, so render the top-level message fields and
        # skip the per-node loop entirely.
        if template_obj.flow.get("mode") == FlowMode.DIRECT.value:
            self._render_direct_mode_flow(template_obj.flow, template_vars)
            logger.info(f"Rendered direct-mode flow for template {template_obj.name}")
            return template_obj, template_vars, _ds_messages

        # IVR mode is a DTMF menu tree (no task/role messages); render the
        # spoken prompt/message fields and skip the per-node task-message loop.
        if template_obj.flow.get("mode") == FlowMode.IVR.value:
            self._render_ivr_flow(template_obj.flow, template_vars)
            logger.info(f"Rendered ivr-mode flow for template {template_obj.name}")
            return template_obj, template_vars

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
        return template_obj, template_vars, _ds_messages
