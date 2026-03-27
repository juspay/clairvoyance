"""
CallResourceManager — centralized resource lifecycle for a single call attempt.

Guarantees cleanup of outbound number channels and Redis greeting keys
via __aexit__, regardless of whether the call succeeds or fails.
"""

from typing import Optional

from app.ai.voice.agents.breeze_buddy.managers.utils import (
    prepare_and_store_initial_greeting,
)
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.core.logger import logger
from app.database.accessor import (
    get_outbound_number_based_on_status_and_provider,
    get_outbound_number_by_id,
    increment_outbound_number_channels,
    update_outbound_number_status,
)
from app.schemas import (
    CallExecutionConfig,
    CallProvider,
    LeadCallTracker,
    OutboundNumber,
    OutboundNumberStatus,
)
from app.services.redis.client import get_redis_service


class CallResourceManager:
    """Manages all resources for a single call attempt.

    Usage:
        async with CallResourceManager(lead) as resources:
            number = await resources.acquire_number(config, template)
            if not number:
                return  # __aexit__ cleans up

            await resources.store_greeting(lead.payload, template)

            # ... make call ...

            # On success: transfer ownership to callback handler
            resources.transfer_ownership()
    """

    def __init__(self, lead: LeadCallTracker):
        self.lead = lead
        self.outbound_number: Optional[OutboundNumber] = None
        self.greeting_stored: bool = False

    async def acquire_number(
        self, config: CallExecutionConfig, template: Optional[TemplateModel]
    ) -> Optional[OutboundNumber]:
        """Acquire an available outbound number. Returns None if none available."""
        number = await _get_available_number(config, template)
        if not number:
            return None

        acquired = await _acquire_number(number)
        if not acquired:
            logger.warning(
                f"Failed to acquire number {number.id} for lead {self.lead.id} — at capacity"
            )
            return None

        self.outbound_number = number
        return number

    async def store_greeting(
        self, payload: dict, template: Optional[TemplateModel]
    ) -> None:
        """Synthesize and store initial greeting audio in Redis."""
        if template:
            await prepare_and_store_initial_greeting(
                lead_id=self.lead.id,
                payload=payload,
                template=template,
            )
            self.greeting_stored = True

    async def release_number(self) -> None:
        """Release outbound number only. Keeps greeting for retry with alternate provider."""
        if self.outbound_number:
            await _release_number(
                self.outbound_number.id, self.outbound_number.provider
            )
            self.outbound_number = None
            await self._signal_channel_freed()

    async def cleanup(self) -> None:
        """Release all acquired resources. Idempotent — safe to call multiple times."""
        had_number = self.outbound_number is not None
        if self.outbound_number:
            await _release_number(
                self.outbound_number.id, self.outbound_number.provider
            )
            self.outbound_number = None

        if self.greeting_stored:
            try:
                redis = await get_redis_service()
                await redis.delete(f"greeting:{self.lead.id}")
            except Exception:
                pass
            self.greeting_stored = False

        if had_number:
            await self._signal_channel_freed()

    def transfer_ownership(self) -> None:
        """Transfer resource ownership to the callback handler.
        After this, __aexit__ will NOT release resources — the callback handler owns them.
        """
        self.outbound_number = None
        self.greeting_stored = False

    async def __aenter__(self) -> "CallResourceManager":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        # Always cleanup on exception. On normal exit, cleanup if ownership wasn't transferred.
        if exc_type is not None or self.outbound_number or self.greeting_stored:
            await self.cleanup()
        return False  # don't suppress exceptions

    @staticmethod
    async def _signal_channel_freed() -> None:
        """Notify dispatcher that a channel was freed so it can grab the next lead."""
        from app.ai.voice.agents.breeze_buddy.managers.lead_dispatcher import (
            get_lead_dispatcher,
        )

        dispatcher = get_lead_dispatcher()
        if dispatcher:
            await dispatcher.on_channel_freed()


# --- Helper functions (moved from calls.py to avoid circular imports) ---


async def _get_available_number(
    config: CallExecutionConfig, template: Optional[TemplateModel]
) -> Optional[OutboundNumber]:
    """Find an available outbound number based on config and template."""
    # Tier 1: Template-specific number
    if template and template.outbound_number_id:
        number = await get_outbound_number_by_id(str(template.outbound_number_id))
        if number and number.status == OutboundNumberStatus.AVAILABLE:
            if not _has_channel_capacity(number):
                return None
            return number
        return None

    # Tier 2: Shared pool (backward compatible)
    numbers = await get_outbound_number_based_on_status_and_provider(
        OutboundNumberStatus.AVAILABLE, config.calling_provider
    )
    if numbers:
        for number in numbers:
            if number.reseller_id is None and number.merchant_id is None:
                if _has_channel_capacity(number):
                    return number
    return None


def _has_channel_capacity(number: OutboundNumber) -> bool:
    """Check if an outbound number has capacity for another call.

    - Exotel: requires channels < maximum_channels (channel-counted)
    - Plivo/Twilio: always available if status=AVAILABLE (no channel counting)
    """
    if number.provider == CallProvider.EXOTEL:
        return (
            number.channels is not None
            and number.maximum_channels is not None
            and number.channels < number.maximum_channels
        )
    # Plivo and Twilio: no channel capacity check needed
    return True


async def _acquire_number(number: OutboundNumber) -> bool:
    """Atomically acquire (increment channels for) an outbound number."""
    if number.provider == CallProvider.TWILIO:
        result = await update_outbound_number_status(
            number.id, OutboundNumberStatus.IN_USE
        )
        return result is not None
    elif number.provider in (CallProvider.EXOTEL, CallProvider.PLIVO):
        result = await increment_outbound_number_channels(number.id)
        return result is not None
    return False


async def _release_number(number_id: str, provider: CallProvider) -> None:
    """Release an outbound number (decrement channels or reset status)."""
    from app.database.accessor import decrement_outbound_number_channels

    if provider == CallProvider.TWILIO:
        await update_outbound_number_status(number_id, OutboundNumberStatus.AVAILABLE)
    elif provider in (CallProvider.EXOTEL, CallProvider.PLIVO):
        await decrement_outbound_number_channels(number_id)
