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

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import (
    HookFieldConfig,
    HookFieldConfigSource,
)
from app.ai.voice.agents.breeze_buddy.utils.common import (
    OUTCOME_TO_ENUM,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    update_lead_call_completion_details,
)
from app.schemas import LeadCallOutcome


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
        expected_fields: Optional[Dict[str, HookFieldConfig]] = None,
    ) -> None:
        """
        Execute the hook logic.

        Args:
            context: Handler context with bot state access
            args: Function arguments from LLM
            function_name: Name of the function that triggered this hook
            expected_fields: Dictionary mapping field names to their HookFieldConfig
        """

    async def safe_execute(
        self,
        context: TemplateContext,
        args: Dict[str, Any],
        function_name: str,
        expected_fields: Optional[Dict[str, HookFieldConfig]] = None,
    ) -> None:
        """
        Safely execute the hook with error handling.

        Args:
            context: Handler context with bot state access
            args: Function arguments from LLM
            function_name: Name of the function that triggered this hook
            expected_fields: Dictionary mapping field names to their HookFieldConfig
        """
        logger.info(
            f"Starting hook '{self.name}' execution for function '{function_name}'"
        )
        try:
            await self.execute(context, args, function_name, expected_fields)
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
        expected_fields: Optional[Dict[str, HookFieldConfig]] = None,
    ) -> None:
        """
        Update the outcome in database.

        Args:
            context: Handler context with bot state access
            args: Function arguments containing outcome and other data from LLM
            function_name: Name of the function that triggered this hook
            expected_fields: Dictionary mapping field names to their HookFieldConfig
        """
        logger.debug(
            f"UpdateOutcomeInDatabaseHook execute called with args: {args}, "
            f"expected_fields: {expected_fields}, for function '{function_name}'"
        )

        # Build final data based on expected_fields
        final_data: Dict[str, Any] = {}

        if expected_fields:
            for field_name, field_config in expected_fields.items():
                if field_config.source == HookFieldConfigSource.STATIC:
                    # Use the enforced value from configuration
                    final_data[field_name] = field_config.value
                    logger.debug(
                        f"Field '{field_name}': using enforced value '{field_config.value}' "
                        f"for function '{function_name}'"
                    )
                elif field_config.source == HookFieldConfigSource.LLM:
                    # Use the value from LLM arguments
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
            logger.warning(
                f"No lead found in context for function '{function_name}'. "
                f"Cannot update outcome in database."
            )
            return

        try:
            # Convert outcome string to enum
            call_outcome = OUTCOME_TO_ENUM.get(outcome, LeadCallOutcome.UNKNOWN)
            logger.debug(
                f"Converted outcome '{outcome}' to enum '{call_outcome.value}' "
                f"for lead {context.lead.id}"
            )

            # Initialize metadata with existing data
            meta_data = context.lead.metaData or {}

            # Add all fields except outcome to metadata
            for key, value in final_data.items():
                if key != "outcome" and value is not None:
                    meta_data[key] = value
                    logger.debug(
                        f"Added '{key}': '{value}' to metadata for lead {context.lead.id}"
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

            # Update lead in database with outcome
            logger.info(
                f"Updating lead {context.lead.id} in database with outcome: {call_outcome.value}, "
                f"metadata: {meta_data}, via function '{function_name}'"
            )

            updated_lead = await update_lead_call_completion_details(
                id=context.lead.id,
                status=None,
                outcome=call_outcome,
                meta_data=meta_data,
                call_end_time=None,
            )

            logger.debug(
                f"update_lead_call_completion_details returned for lead {context.lead.id}: "
                f"{updated_lead}"
            )

            if updated_lead:
                # Update the lead in context so subsequent hook calls have the latest metadata
                context.bot.lead = updated_lead
                logger.info(
                    f"Successfully updated outcome in database for lead {context.lead.id}: "
                    f"{call_outcome.value} (function: '{function_name}') and refreshed context.lead"
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
