"""
Outbound number management for call operations.
"""

from typing import Optional

from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.core.logger import logger
from app.database.accessor import (
    get_outbound_number_based_on_status_and_provider,
    get_outbound_number_by_id,
    update_outbound_number_channels,
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
    Falls back to backward compatible approach (matching by merchant/shop).
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

    else:
        logger.info(
            f"Using backward compatible approach: looking for outbound number "
            f"matching merchant {config.merchant_id}, shop {config.shop_identifier}"
        )

        # Get all available numbers
        all_available_numbers = await get_outbound_number_based_on_status_and_provider(
            OutboundNumberStatus.AVAILABLE, config.calling_provider
        )

        # Filter by merchant_id and shop_identifier as none for fallback
        matching_numbers = [
            n
            for n in all_available_numbers
            if n.merchant_id is None and n.shop_identifier is None
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
        logger.warning(
            f"No outbound number found for merchant {config.merchant_id}, "
            f"template {config.template}, shop {config.shop_identifier}"
        )
        return None

    logger.info(
        f"Using outbound number {number.number} (provider: {number.provider}) "
        f"for template {config.template}, merchant {config.merchant_id}, shop {config.shop_identifier}"
    )
    return number


async def get_available_number_by_provider(
    provider: CallProvider,
) -> Optional[OutboundNumber]:
    """
    Finds an available outbound number for a specific provider.
    Used for retry scenarios with provider fallback.
    """
    retry_numbers = await get_outbound_number_based_on_status_and_provider(
        OutboundNumberStatus.AVAILABLE, provider
    )

    if not retry_numbers:
        return None

    for number in retry_numbers:
        if number.merchant_id is None and number.shop_identifier is None:
            if provider == CallProvider.EXOTEL:
                if (
                    number.channels is not None
                    and number.maximum_channels is not None
                    and number.channels < number.maximum_channels
                ):
                    return number
            else:
                return number

    return None


async def acquire_number(number: OutboundNumber):
    """
    Marks an outbound number as in use.
    """
    if number.provider == CallProvider.TWILIO:
        await update_outbound_number_status(number.id, OutboundNumberStatus.IN_USE)
    elif number.provider == CallProvider.EXOTEL:
        if number.channels is not None:
            await update_outbound_number_channels(number.id, number.channels + 1)
        else:
            logger.warning(
                f"Cannot acquire Exotel number {number.id}: channels is None"
            )


async def release_number(number_id: str, provider: CallProvider):
    """
    Releases an outbound number, making it available for other calls.
    """
    if provider == CallProvider.TWILIO:
        await update_outbound_number_status(number_id, OutboundNumberStatus.AVAILABLE)
    elif provider == CallProvider.EXOTEL:
        outbound_number = await get_outbound_number_by_id(number_id)
        if outbound_number and outbound_number.channels is not None:
            await update_outbound_number_channels(
                number_id, outbound_number.channels - 1
            )
        elif outbound_number and outbound_number.channels is None:
            logger.warning(
                f"Cannot release Exotel number {number_id}: channels is None"
            )
