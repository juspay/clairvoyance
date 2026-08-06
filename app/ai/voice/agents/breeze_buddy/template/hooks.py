"""
Hook System for Workflow Engine

Hooks are asynchronous side-effect handlers that execute after a function is called.
They run independently and don't block the workflow transition.

Example:
    - cancel_order function → update_outcome_in_database hook
    - not_available function → retry hook
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester import (
    HttpRequestExecutor,
)
from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.computed_fields import (
    resolve_computed_value,
)
from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.field_resolver import (
    FieldResolver,
)
from app.ai.voice.agents.breeze_buddy.services.credential_auth import (
    resolve_credential_auth,
    resolve_credential_scope,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import (
    FieldSource,
    HookConfig,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.chat_session import (
    update_chat_session_outcome,
)
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    update_lead_call_completion_details,
)


class Hook(ABC):
    """
    Base class for all hooks.

    Hooks are asynchronous operations that execute as side effects
    after a function is triggered, without blocking the main workflow.
    """

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    async def execute(
        self,
        context: TemplateContext,
        args: Dict[str, Any],
        function_name: str,
        hook_config: HookConfig,
    ) -> None:
        """
        Execute the hook logic.

        Args:
            context: Handler context with bot state access
            args: Function arguments from LLM
            function_name: Name of the function that triggered this hook
            hook_config: Complete hook configuration - each hook extracts what it needs
        """

    async def safe_execute(
        self,
        context: TemplateContext,
        args: Dict[str, Any],
        function_name: str,
        hook_config: HookConfig,
    ) -> None:
        """
        Safely execute the hook with error handling.

        Args:
            context: Handler context with bot state access
            args: Function arguments from LLM
            function_name: Name of the function that triggered this hook
            hook_config: Complete hook configuration - each hook extracts what it needs
        """
        logger.info(
            f"Starting hook '{self.name}' execution for function '{function_name}'"
        )
        try:
            await self.execute(context, args, function_name, hook_config)
            logger.info(
                f"Successfully completed hook '{self.name}' for function '{function_name}'"
            )
        except Exception as e:
            logger.error(
                f"Error in hook '{self.name}' for function '{function_name}': {str(e)}",
                exc_info=True,
            )


class UpdateOutcomeInDatabaseHook(Hook):
    """
    Hook to update outcome in database.

    This hook updates the call outcome in the database asynchronously.
    """

    def __init__(self):
        super().__init__("update_outcome_in_database")

    async def execute(
        self,
        context: TemplateContext,
        args: Dict[str, Any],
        function_name: str,
        hook_config: HookConfig,
    ) -> None:
        """
        Update the outcome in database.

        Args:
            context: Handler context with bot state access
            args: Function arguments containing outcome and other data from LLM
            function_name: Name of the function that triggered this hook
            hook_config: Complete hook configuration - this hook extracts expected_fields
        """
        # Extract what this hook needs from hook_config
        expected_fields = hook_config.expected_fields

        logger.debug(
            f"UpdateOutcomeInDatabaseHook execute called with args: {args}, "
            f"expected_fields: {expected_fields}, for function '{function_name}'"
        )

        # Build final data based on expected_fields
        final_data: Dict[str, Any] = {}

        if expected_fields:
            for field_name, field_config in expected_fields.items():
                if field_config.source == FieldSource.STATIC:
                    final_data[field_name] = field_config.value
                    logger.debug(
                        f"Field '{field_name}': using enforced value '{field_config.value}' "
                        f"for function '{function_name}'"
                    )
                elif field_config.source == FieldSource.LLM:
                    value = args.get(field_name)
                    if value is not None:
                        final_data[field_name] = value
                        logger.debug(
                            f"Field '{field_name}': using LLM-inferred value '{value}' "
                            f"for function '{function_name}'"
                        )
                    else:
                        logger.warning(
                            f"Field '{field_name}': type is 'llm' but no value found in args "
                            f"for function '{function_name}'. Args: {args}"
                        )
                elif field_config.source == FieldSource.COMPUTED:
                    if field_config.value:
                        try:
                            computed_value = resolve_computed_value(field_config.value)
                            final_data[field_name] = computed_value
                            logger.debug(
                                f"Field '{field_name}': using computed value '{computed_value}' "
                                f"for function '{function_name}'"
                            )
                        except ValueError as e:
                            logger.error(
                                f"Field '{field_name}': failed to resolve computed value "
                                f"'{field_config.value}' for function '{function_name}': {e}"
                            )
                    else:
                        logger.warning(
                            f"Field '{field_name}': COMPUTED source requires 'value' field "
                            f"for function '{function_name}'"
                        )
        else:
            # Fallback to old behavior if no expected_fields provided
            logger.warning(
                f"No expected_fields provided for hook, falling back to extracting from args "
                f"for function '{function_name}'"
            )
            final_data = args.copy()

        logger.info(
            f"Final data to be processed: {final_data} for function '{function_name}'"
        )

        # Extract outcome
        outcome = final_data.get("outcome")

        if not outcome:
            logger.warning(
                f"No outcome provided in final data for function '{function_name}'. "
                f"Skipping database update. Final data: {final_data}"
            )
            return

        logger.info(
            f"Extracted outcome '{outcome}' from final data for function '{function_name}'"
        )

        # Get lead from context
        if not context.lead or not context.lead.id:
            # Chat / assist mode (no lead by design): persist the singular
            # outcome to chat_session.outcome instead of dropping it. The
            # session id rides on the bot (ChatAgent.session_id); when even
            # that is absent (e.g. a bare test harness) there is nothing to
            # write to. Mirroring to a bound voice lead is a follow-up.
            session_id = getattr(context.bot, "session_id", None)
            if not session_id:
                logger.info(
                    f"No lead and no chat session for '{function_name}': "
                    f"skipping outcome persistence."
                )
                return
            try:
                await update_chat_session_outcome(str(session_id), str(outcome))
                logger.info(
                    f"Persisted chat outcome '{outcome}' to session {session_id} "
                    f"(function: '{function_name}')"
                )
            except Exception as e:
                logger.opt(exception=True).error(
                    f"Failed to persist chat outcome '{outcome}' to session "
                    f"{session_id} (function: '{function_name}'): {e}"
                )
            return

        try:
            logger.debug(f"outcome '{outcome}' for lead {context.lead.id}")

            # Initialize metadata with existing data
            meta_data = context.lead.metaData or {}

            # Initialize outcome object inside metadata if it doesn't exist
            if "outcome" not in meta_data:
                meta_data["outcome"] = {}

            # Add all other fields except outcome to the outcome object
            for key, value in final_data.items():
                if key != "outcome" and value is not None:
                    meta_data["outcome"][key] = value
                    logger.debug(
                        f"Added '{key}': '{value}' to outcome in metadata for lead {context.lead.id}"
                    )

            # Log all metadata properties that were added
            if meta_data:
                logger.debug(
                    f"Properties added to metadata: {list(meta_data.keys())} "
                    f"for lead {context.lead.id}"
                )
            else:
                logger.debug(
                    f"No additional properties found in final data for lead {context.lead.id}"
                )

            # Guard: if a transfer is in progress, preserve "TRANSFERRED" outcome
            # instead of the LLM's value (e.g., "RESOLVED") to avoid a race
            # condition with handle_call_completion's transfer override.
            if meta_data.get("transfer", {}).get("status") == "success":
                logger.info(
                    f"Transfer detected for lead {context.lead.id}. "
                    f"Overriding outcome from '{outcome}' to 'TRANSFERRED' "
                    f"(function: '{function_name}')"
                )
                outcome = "TRANSFERRED"

            # Guard: if an observer already set the outcome, preserve it.
            # The observer runs in parallel and may detect voicemail before
            # the main LLM calls user_busy — don't let user_busy overwrite.
            if meta_data.get("observer_triggered") and context.lead.outcome:
                existing = context.lead.outcome
                if existing != outcome:
                    logger.info(
                        f"Observer already set outcome '{existing}' for lead "
                        f"{context.lead.id}. Preserving over '{outcome}' "
                        f"(function: '{function_name}')"
                    )
                    outcome = existing

            # Set in-memory outcome AFTER transfer override but BEFORE the
            # async DB write.  This prevents a race condition in Direct Mode
            # where end_conversation_global checks context.lead.outcome and
            # defaults to BUSY because the async DB write hasn't completed yet.
            context.lead.outcome = outcome

            # Update lead in database with outcome
            logger.info(
                f"Updating lead {context.lead.id} in database with outcome: {outcome}, "
                f"metadata: {meta_data}, via function '{function_name}'"
            )

            updated_lead = await update_lead_call_completion_details(
                id=context.lead.id,
                status=None,
                outcome=outcome,
                meta_data=meta_data,
                call_end_time=None,
            )

            logger.debug(
                f"update_lead_call_completion_details returned for lead {context.lead.id}: "
                f"{updated_lead}"
            )

            if updated_lead:
                # Update the lead in context so subsequent hook calls have the latest metadata
                context.lead = updated_lead
                logger.info(
                    f"Successfully updated outcome in database for lead {context.lead.id}: "
                    f"{outcome} (function: '{function_name}') and refreshed context.lead"
                )
            else:
                logger.error(
                    f"Failed to update outcome in database for lead {context.lead.id}. "
                    f"update_lead_call_completion_details returned None (function: '{function_name}')"
                )

        except Exception as e:
            logger.error(
                f"Error updating outcome in database for lead {context.lead.id} "
                f"(function: '{function_name}'): {e}",
                exc_info=True,
            )


class ExternalHTTPHook(Hook):
    """
    Hook to send HTTP requests to external APIs.

    This hook executes HTTP requests asynchronously without blocking the conversation.
    Supports all HTTP methods, authentication, headers, query params, and body.

    Example use cases:
        - Send webhook to merchant CRM
        - Update external order management system
        - Send analytics events
        - Trigger third-party integrations
    """

    def __init__(self):
        super().__init__("send_http_request")

    async def execute(
        self,
        context: TemplateContext,
        args: Dict[str, Any],
        function_name: str,
        hook_config: HookConfig,
    ) -> None:
        """
        Execute HTTP request with simple value resolution.

        Args:
            context: Handler context with bot state access (includes aiohttp_session)
            args: Function arguments from LLM
            function_name: Name of the function that triggered this hook
            hook_config: Complete hook configuration - this hook extracts http_request
        """
        logger.debug(f"ExternalHTTPHook execute called for function '{function_name}'")

        # Extract what THIS hook needs
        http_request = hook_config.http_request

        if not http_request:
            logger.error(
                f"ExternalHTTPHook requires http_request config for function '{function_name}'"
            )
            return

        if not context.aiohttp_session:
            logger.error(
                f"No aiohttp_session available in context for function '{function_name}'"
            )
            return

        # Resolve expected_fields to get dynamic values
        resolver = FieldResolver(context=context, args=args)
        resolved_fields: Dict[str, Any] = {}

        if hook_config.expected_fields:
            logger.debug(
                f"Resolving {len(hook_config.expected_fields)} expected_fields "
                f"for function '{function_name}'"
            )
            for field_name, field_config in hook_config.expected_fields.items():
                resolved_value = resolver.resolve_value(
                    field_config, field_name=field_name
                )
                if resolved_value is not None:
                    resolved_fields[field_name] = resolved_value
                    logger.debug(
                        f"Resolved field '{field_name}' = '{resolved_value}' "
                        f"from source '{field_config.source}'"
                    )

        logger.debug(
            f"Resolved HTTP field names: {list(resolved_fields.keys())} "
            f"for function '{function_name}'"
        )

        template = getattr(context.bot, "template", None)
        reseller_id, merchant_id = resolve_credential_scope(template, context.lead)
        resolved_auth = await resolve_credential_auth(
            http_request.auth,
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            credential_cache=context.credential_cache,
        )
        http_request = http_request.model_copy(update={"auth": resolved_auth})

        # Create executor
        executor = HttpRequestExecutor(session=context.aiohttp_session)

        logger.info(
            f"Executing HTTP {http_request.method.value} request to {http_request.url} "
            f"for function '{function_name}'"
        )

        await executor.execute(config=http_request, resolved_fields=resolved_fields)

        logger.info(f"HTTP request completed for function '{function_name}'")


class HookRegistry:
    """
    Registry for all available hooks.

    This class maintains a mapping of hook names to hook instances.
    """

    _hooks: Dict[str, Hook] = {}

    @classmethod
    def register(cls, name: str, hook: Hook) -> None:
        """
        Register a hook.

        Args:
            name: Name to register the hook under
            hook: Hook instance
        """
        cls._hooks[name] = hook

    @classmethod
    def get(cls, name: str) -> Optional[Hook]:
        """
        Get a hook by name.

        Args:
            name: Name of the hook

        Returns:
            Hook instance or None if not found
        """
        return cls._hooks.get(name)

    @classmethod
    def get_all(cls) -> Dict[str, Hook]:
        """
        Get all registered hooks.

        Returns:
            Dictionary of all hooks
        """
        return cls._hooks.copy()


# Register hooks
HookRegistry.register("update_outcome_in_database", UpdateOutcomeInDatabaseHook())
HookRegistry.register("send_http_request", ExternalHTTPHook())
