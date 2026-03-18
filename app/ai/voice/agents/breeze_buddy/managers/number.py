"""
Outbound number management for call operations.
Handles acquisition and release of outbound numbers.
"""

from typing import Optional

from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.core.logger import logger
from app.database.accessor import (
    decrement_outbound_number_channels,
    get_outbound_number_based_on_status_and_provider,
    get_outbound_number_by_id,
    increment_outbound_number_channels,
    update_outbound_number_status,
)
from app.schemas import (
    CallExecutionConfig,
    CallProvider,
    OutboundNumber,
    OutboundNumberStatus,
)


async def get_available_number(
    config: CallExecutionConfig,
    template: Optional[TemplateModel],
) -> Optional[OutboundNumber]:
    """
    Finds an available outbound number for a given configuration.

    First tries the new approach (template with outbound_number_id).
    Falls back to backward compatible approach (matching by reseller/shop).
    """

    number = None

    if template and template.outbound_number_id:
        logger.info(
            f"Using new approach: template {config.template} has outbound_number_id {template.outbound_number_id}"
        )
        outbound_number = await get_outbound_number_by_id(template.outbound_number_id)

        if outbound_number and outbound_number.status == OutboundNumberStatus.AVAILABLE:
            if outbound_number.provider == CallProvider.EXOTEL:
                if (
                    outbound_number.channels is not None
                    and outbound_number.maximum_channels is not None
                    and outbound_number.channels < outbound_number.maximum_channels
                ):
                    number = outbound_number
            elif outbound_number.provider == CallProvider.TWILIO:
                number = outbound_number
            elif outbound_number.provider == CallProvider.PLIVO:
                number = outbound_number

    else:
        logger.info(
            f"Using backward compatible approach: looking for outbound number "
            f"matching reseller {config.reseller_id}, shop {config.merchant_id}"
        )

        # Get all available numbers
        all_available_numbers = await get_outbound_number_based_on_status_and_provider(
            OutboundNumberStatus.AVAILABLE, config.calling_provider
        )

        # Filter by reseller_id and merchant_id as none for fallback
        matching_numbers = [
            n
            for n in all_available_numbers
            if n.reseller_id is None and n.merchant_id is None
        ]

        if matching_numbers:
            for num in matching_numbers:
                if num.provider == CallProvider.EXOTEL:
                    if (
                        num.channels is not None
                        and num.maximum_channels is not None
                        and num.channels < num.maximum_channels
                    ):
                        number = num
                        break
                else:
                    number = num
                    break

    if not number:
        # Support both new and old field names for config
        config_reseller_id = config.reseller_id
        config_merchant_id = config.merchant_id
        logger.warning(
            f"No outbound number found for reseller {config_reseller_id}, "
            f"template {config.template}, shop {config_merchant_id}"
        )
        return None

    logger.info(
        f"Using outbound number {number.number} (provider: {number.provider}) "
        f"for template {config.template}, reseller {config.reseller_id}, shop {config.merchant_id}"
    )
    return number


async def acquire_number(number: OutboundNumber) -> bool:
    """
    Marks an outbound number as in use.
    Uses atomic increment to avoid race conditions.
    For Exotel, only succeeds if channels < maximum_channels.
    Returns True if acquisition succeeded, False if at capacity.
    """
    if number.provider == CallProvider.TWILIO:
        result = await update_outbound_number_status(
            number.id, OutboundNumberStatus.IN_USE
        )
        return result is not None
    elif number.provider == CallProvider.EXOTEL:
        result = await increment_outbound_number_channels(number.id)
        return result is not None
    elif number.provider == CallProvider.PLIVO:
        result = await increment_outbound_number_channels(number.id)
        return result is not None
    return False


async def release_number(number_id: str, provider: CallProvider):
    """
    Releases an outbound number, making it available for other calls.
    Uses atomic decrement to avoid race conditions.
    """
    if provider == CallProvider.TWILIO:
        await update_outbound_number_status(number_id, OutboundNumberStatus.AVAILABLE)
    elif provider == CallProvider.EXOTEL:
        await decrement_outbound_number_channels(number_id)
    elif provider == CallProvider.PLIVO:
        await decrement_outbound_number_channels(number_id)


async def get_retry_number(
    retry_calling_provider: CallProvider,
) -> Optional[OutboundNumber]:
    """
    Gets an available number for retry with a different provider.
    Returns None if no suitable number is found.
    """
    retry_numbers = await get_outbound_number_based_on_status_and_provider(
        OutboundNumberStatus.AVAILABLE, retry_calling_provider
    )

    if not retry_numbers:
        return None

    for number in retry_numbers:
        if number.reseller_id is None and number.merchant_id is None:
            if retry_calling_provider == CallProvider.EXOTEL:
                if (
                    number.channels is not None
                    and number.maximum_channels is not None
                    and number.channels < number.maximum_channels
                ):
                    return number
            else:
                return number

    return None
