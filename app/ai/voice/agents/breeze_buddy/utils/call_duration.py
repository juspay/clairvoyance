"""Max call duration cap for voice calls (ConfigurationModel.max_call_duration_minutes).

One timer per call, started in Agent.run() and cancelled in its finally, so
the budget spans agent-to-agent transfers. It never ends a call mid-handoff:
a human transfer being dialled or a hold & consult (``during_handoff``), or an
agent-to-agent rebuild before the next pipeline task exists.
"""

import asyncio
import functools
import time
from contextlib import contextmanager
from typing import Any, Awaitable, Callable, Iterator, TypeVar

from app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation import (
    end_conversation,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import (
    DEFAULT_MAX_CALL_DURATION_MINUTES,
)
from app.core.logger import logger

_Handler = TypeVar("_Handler", bound=Callable[..., Awaitable[Any]])


@contextmanager
def handoff_in_progress(bot: Any) -> Iterator[None]:
    """Defer the cap while the block runs (nestable)."""
    bot.handoff_depth = getattr(bot, "handoff_depth", 0) + 1
    try:
        yield
    finally:
        bot.handoff_depth -= 1


def during_handoff(handler: _Handler) -> _Handler:
    """Run a ``(context, ...)`` handler under ``handoff_in_progress``."""

    @functools.wraps(handler)
    async def wrapper(context: Any, *args: Any, **kwargs: Any) -> Any:
        with handoff_in_progress(context.bot):
            return await handler(context, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


def max_call_duration_seconds(configurations: Any) -> float:
    minutes = getattr(
        configurations, "max_call_duration_minutes", DEFAULT_MAX_CALL_DURATION_MINUTES
    )
    return float(minutes) * 60.0


def _handoff_active(bot: Any) -> bool:
    return bool(
        bot.pending_transfer
        or getattr(bot, "_between_generations", False)
        or getattr(bot, "handoff_depth", 0) > 0
    )


async def enforce_max_call_duration(bot: Any) -> None:
    """Sleep out the budget, wait out any handoff, then end the call."""
    max_seconds = max_call_duration_seconds(bot.configurations)
    # Epoch deadline, handed to the provider on warm transfer so the human
    # part of the call is capped too (utils/warm_transfer.py).
    bot.max_call_end_at = time.time() + max_seconds
    logger.info(f"Max call duration for {bot.call_sid}: {int(max_seconds)}s")
    try:
        await asyncio.sleep(max_seconds)
        while _handoff_active(bot) and not bot.conversation_ended:
            await asyncio.sleep(1.0)
        # Shielded so run()'s teardown can't cut finalization off midway.
        await asyncio.shield(_end_call(bot, max_seconds))
    except asyncio.CancelledError:
        logger.debug("Max call duration timer cancelled.")
    except Exception:
        logger.exception("Failed to end call on max call duration")


def mark_max_duration_end(lead: Any, max_seconds: float) -> None:
    """Record a cap-ended call on the lead; keeps an outcome the flow already set."""
    if lead.outcome is None:
        lead.outcome = "BUSY"
    if lead.metaData is None:
        lead.metaData = {}
    lead.metaData["call_ended_by"] = "system"
    lead.metaData["call_end_reason"] = "max_call_duration_exceeded"
    lead.metaData["max_call_duration_seconds"] = int(max_seconds)


async def _end_call(bot: Any, max_seconds: float) -> None:
    """System-end via end_conversation."""
    if bot.conversation_ended:
        return
    if bot.lead:
        mark_max_duration_end(bot.lead, max_seconds)
        if bot._transcript_collector:
            bot.lead.metaData["transcription"] = (
                bot._transcript_collector.get_transcription()
            )
    logger.info(f"Ending call {bot.call_sid}: max call duration reached")
    await end_conversation(TemplateContext(bot), {})
