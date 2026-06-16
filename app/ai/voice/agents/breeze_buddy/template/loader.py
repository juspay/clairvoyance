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
    DataSourceMode,
    DataSourceRef,
    FlowMode,
    TemplateModel,
)
from app.ai.voice.agents.breeze_buddy.template.utils import render_messages_with_vars
from app.core.logger import logger
from app.database.accessor.breeze_buddy.credentials import (
    get_credentials_as_template_vars,
)
from app.database.accessor.breeze_buddy.template import (
    get_template_by_id_with_fallback,
)
from app.services.data_sources import (
    DATA_SOURCE_UNAVAILABLE,
    get_connector,
    within_cache_limit,
)
from app.services.redis import get_redis_service


def eager_data_sources(
    data_sources: Optional[List[DataSourceRef]],
) -> List[DataSourceRef]:
    """Sources injected statically up front.

    ``on_demand`` sources are deliberately excluded: they are loaded one keyed
    slice at a time mid-call via the ``load_data_source`` global function, never
    dumped wholesale into the prompt.
    """
    return [ref for ref in (data_sources or []) if ref.mode != DataSourceMode.ON_DEMAND]


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

    def render_task_messages(
        self, task_messages: list, variables: Dict[str, str]
    ) -> list:
        """Method-style alias preserved for the voice loader call sites."""
        return render_messages_with_vars(task_messages, variables)

    async def load_template(
        self,
        reseller_id: str,
        template: str,
        merchant_id: Optional[str] = None,
        call_payload: Optional[Dict[str, str]] = None,
        template_id: Optional[str] = None,
    ) -> Tuple[TemplateModel, Dict[str, str]]:
        """
        Load template and render task messages with variables.

        Args:
            reseller_id: Reseller identifier
            template: Template name (used as fallback if template_id not provided)
            merchant_id: Optional merchant-specific identifier
            call_payload: Optional payload variables
            template_id: Optional template UUID (preferred over name-based lookup)

        Returns:
            TemplateModel with rendered task messages, and dictionary of template variables

        Raises:
            ValueError: If template not found
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

        # 5. data sources — inject EAGER sources as {datasource_<name>} variables.
        # on_demand sources are skipped here; the agent loads them one keyed
        # slice at a time mid-call via the load_data_source global function.
        eager_sources = eager_data_sources(template_obj.data_sources)
        if eager_sources:
            contents = await asyncio.gather(
                *[self._fetch_one_data_source(ref) for ref in eager_sources],
                return_exceptions=True,
            )
            for ref, result in zip(eager_sources, contents):
                # Skip exceptions, None, and the failure sentinel — never inject
                # "[Data unavailable]" into the prompt as if it were content.
                if (
                    not isinstance(result, Exception)
                    and result is not None
                    and result != DATA_SOURCE_UNAVAILABLE
                ):
                    var_key = f"datasource_{ref.name}"
                    if var_key not in template_vars:
                        template_vars[var_key] = result
                        logger.info(
                            f"Injected data source '{ref.name}' as variable '{var_key}'"
                        )
        # Direct mode has a flat structure (system_prompt + flat function list)
        # rather than a nodes array, so render the top-level message fields and
        # skip the per-node loop entirely.
        if template_obj.flow.get("mode") == FlowMode.DIRECT.value:
            self._render_direct_mode_flow(template_obj.flow, template_vars)
            logger.info(f"Rendered direct-mode flow for template {template_obj.name}")
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
        return template_obj, template_vars

    async def _fetch_one_data_source(self, ref: "DataSourceRef") -> str:
        if not ref.is_active:
            return DATA_SOURCE_UNAVAILABLE

        # Route through the connector registry (same path as on-demand) so the
        # cache key and fetch logic have a single source of truth, and so any
        # connector type works — not just google_sheet.
        connector = get_connector(ref.type)
        if connector is None:
            logger.warning(
                f"Data source '{ref.name}': no connector for type '{ref.type}'"
            )
            return DATA_SOURCE_UNAVAILABLE

        config = ref.connector_config()
        # Eager sources address a fixed slice: the configured sheet (in config).
        cache_key = connector.cache_key(config, None)

        try:
            redis = await get_redis_service()
            cached = await redis.get(cache_key)
            if cached:
                return cached
        except Exception:
            pass

        try:
            content = await asyncio.wait_for(connector.fetch(config, None), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(f"Data source '{ref.name}' fetch timed out")
            return DATA_SOURCE_UNAVAILABLE
        except Exception as exc:
            logger.error(f"Data source '{ref.name}' fetch failed: {exc}", exc_info=True)
            return DATA_SOURCE_UNAVAILABLE

        if content != DATA_SOURCE_UNAVAILABLE and within_cache_limit(content):
            try:
                redis = await get_redis_service()
                await redis.setex(cache_key, content, ttl_seconds=60)
            except Exception:
                pass

        return content
