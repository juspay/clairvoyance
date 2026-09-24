"""Indian Number to Speech Handler

Built-in global function that converts a numeric price (in rupees) to its
Indian-words spoken form. Registered as an LLM-callable builtin so the agent
spells out a product price only when it is about to state it — instead of
pre-converting every price in a catalogue result.
"""

from typing import Any, Dict

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.transformation_function.utils import (
    indian_number_to_speech,
)
from app.core.logger import logger


async def indian_number_to_speech_handler(
    context: TemplateContext,
    args: Dict[str, Any],
) -> Dict[str, Any]:
    """Convert an amount in rupees to Indian words and return the spoken form.

    Args:
        context: Handler context with bot state access (unused).
        args: LLM function arguments, must contain a numeric ``price`` in rupees.

    Returns:
        Dict with the spoken phrase, or an error dict on missing/invalid input.
    """
    price = args.get("price")
    if price is None:
        logger.warning("[indian_number_to_speech] missing 'price' argument")
        return {"status": "error", "error": "Missing required argument 'price'."}

    try:
        spoken = indian_number_to_speech(float(price))
    except (TypeError, ValueError) as e:
        logger.warning(f"[indian_number_to_speech] invalid price {price!r}: {e}")
        return {
            "status": "error",
            "error": f"Invalid price {price!r}: expected a number.",
        }

    logger.info(
        f"[indian_number_to_speech] {price!r} -> '{spoken}' for call {context.call_sid}"
    )
    return {"status": "success", "price_in_words": spoken}
