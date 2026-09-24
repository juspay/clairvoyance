"""One research run as a stream of events, and how many may run at once."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator, Dict, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.assist.engine.research import agent
from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingConfigurationError,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    EgressNotGuardedError,
    FetchFailedError,
    UnsafeUrlError,
)
from app.core.logger import logger

Event = Tuple[str, Dict[str, Any]]


class RunsBusyError(Exception):
    """No slot free: ``scope`` is "worker" (everyone's) or "user" (the caller's)."""

    def __init__(self, scope: str) -> None:
        super().__init__(scope)
        self.scope = scope


class Slot:
    """One claimed run. Releasing it twice is a no-op."""

    def __init__(self, slots: RunSlots, user_id: str) -> None:
        self._slots = slots
        self._user_id = user_id
        self._held = True

    def release(self) -> None:
        if self._held:
            self._held = False
            self._slots._drop(self._user_id)


class RunSlots:
    """Runs at once on this worker, in total and per user.

    ``claim`` checks and takes a slot in one step, with no await between, so
    requests arriving together cannot all pass the check before any takes one.
    """

    def __init__(self, total: int, per_user: int) -> None:
        self._total = total
        self._per_user = per_user
        self._held: Dict[str, int] = {}

    def claim(self, user_id: str) -> Slot:
        if sum(self._held.values()) >= self._total:
            raise RunsBusyError("worker")
        if self._held.get(user_id, 0) >= self._per_user:
            raise RunsBusyError("user")
        self._held[user_id] = self._held.get(user_id, 0) + 1
        return Slot(self, user_id)

    def held(self) -> Dict[str, int]:
        return dict(self._held)

    def _drop(self, user_id: str) -> None:
        left = self._held.get(user_id, 0) - 1
        if left > 0:
            self._held[user_id] = left
        else:
            self._held.pop(user_id, None)


async def research_events(url: str) -> AsyncGenerator[Event, None]:
    """Research ``url``: ``progress`` and ``note`` events, then exactly one of
    ``done`` or ``error``. Closing the stream early cancels the run."""
    queue: "asyncio.Queue[Optional[Event]]" = asyncio.Queue()

    async def emit(event: str, data: Dict[str, Any]) -> None:
        await queue.put((event, data))

    async def run() -> None:
        try:
            await emit(
                "progress",
                {"step": "researching", "status": "running", "detail": "Starting"},
            )
            outcome = await agent.research(url, on_event=emit)
            await emit(
                "progress",
                {
                    "step": "researching",
                    "status": "done",
                    "detail": outcome.stopped_because,
                },
            )
            await emit(
                "done",
                {
                    "notes": [
                        {
                            "field": note.field_name,
                            "value": note.value,
                            "source_url": note.source_url,
                        }
                        for note in outcome.evidence.notes
                    ],
                    "pages_read": len(outcome.evidence.readable()),
                },
            )
        except (UnsafeUrlError, FetchFailedError) as exc:
            logger.info(f"assist research could not read the site: {exc}")
            await emit(
                "error",
                {
                    "code": "unreadable_site",
                    "message": "We could not read that website.",
                    "retryable": False,
                },
            )
        except (EgressNotGuardedError, WebsiteScrapingConfigurationError) as exc:
            logger.error(f"assist research unavailable: {exc}")
            await emit(
                "error",
                {
                    "code": "unavailable",
                    "message": "Research is not available right now.",
                    "retryable": False,
                },
            )
        except Exception as exc:
            logger.error(f"assist research failed: {exc}")
            await emit(
                "error",
                {
                    "code": "research_failed",
                    "message": "Research could not be completed.",
                    "retryable": True,
                },
            )
        finally:
            await queue.put(None)

    worker = asyncio.create_task(run())
    try:
        while (item := await queue.get()) is not None:
            yield item
    finally:
        # The browser went away: stop reading someone's site for nobody.
        if not worker.done():
            worker.cancel()


__all__ = ["RunSlots", "RunsBusyError", "Slot", "research_events"]
