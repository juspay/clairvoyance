"""The console's store research: one run as a stream of events, and how many
may run at once."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator, Dict, Literal, Optional

from app.ai.voice.agents.breeze_buddy.assist.engine.research import site
from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingConfigurationError,
    WebsiteScrapingUpstreamError,
)
from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent
from app.core.logger import logger
from app.schemas.breeze_buddy.assist.research import (
    ResearchDone,
    ResearchError,
    ResearchErrorCode,
    ResearchNote,
)

# Seconds a stream may go quiet before a ping is sent; proxies close an idle
# stream, and rendering one page can take longer than this.
PING_SECONDS = 15.0


class RunsBusyError(Exception):
    """No slot free: ``scope`` is "worker" (everyone's) or "user" (the caller's)."""

    def __init__(self, scope: Literal["worker", "user"]) -> None:
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


async def research_events(url: str) -> AsyncGenerator[SSEEvent, None]:
    """Research ``url``: ``progress``, ``note`` and ``ping`` events, then
    exactly one of ``done`` or ``error``. Closing the stream early cancels the
    run."""
    queue: "asyncio.Queue[Optional[SSEEvent]]" = asyncio.Queue()

    async def on_event(kind: str, data: Dict[str, Any]) -> None:
        if kind == "progress":
            await queue.put(_progress(data["detail"]))
        elif kind == "note":
            await queue.put(
                SSEEvent(event="note", data=ResearchNote(**data).model_dump())
            )

    async def run() -> None:
        try:
            await queue.put(_progress("Starting"))
            outcome = await site.research(url, on_event=on_event)
            done = ResearchDone(
                notes=[
                    ResearchNote(
                        field=note.field,
                        value=note.value,
                        source_url=note.source_url,
                    )
                    for note in outcome.notes
                ],
                pages_read=outcome.pages_read,
                stopped_because=outcome.stopped_because,
            )
            logger.info(
                "assist research complete",
                stopped_because=done.stopped_because,
                notes=len(done.notes),
                pages_read=done.pages_read,
            )
            await queue.put(SSEEvent(event="done", data=done.model_dump()))
        except WebsiteScrapingUpstreamError as exc:
            logger.info(f"assist research could not read the site: {exc}")
            await queue.put(
                _error("UNREADABLE_SITE", "We could not read that website.")
            )
        except WebsiteScrapingConfigurationError as exc:
            logger.error(f"assist research unavailable: {exc}")
            await queue.put(
                _error("RESEARCH_UNAVAILABLE", "Research is not available right now.")
            )
        except Exception:
            logger.exception("assist research failed")
            await queue.put(
                _error(
                    "RESEARCH_FAILED",
                    "Research could not be completed.",
                    retryable=True,
                )
            )
        finally:
            await queue.put(None)

    worker = asyncio.create_task(run())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), PING_SECONDS)
            except TimeoutError:
                yield SSEEvent(event="ping")
                continue
            if item is None:
                break
            yield item
    finally:
        # The browser went away: stop reading someone's site for nobody.
        if not worker.done():
            logger.info("assist research: client disconnected; run cancelled")
            worker.cancel()


def _progress(detail: str) -> SSEEvent:
    return SSEEvent(
        event="progress",
        data={"step": "researching", "status": "running", "detail": detail},
    )


def _error(
    code: ResearchErrorCode, message: str, *, retryable: bool = False
) -> SSEEvent:
    error = ResearchError(code=code, message=message, retryable=retryable)
    return SSEEvent(event="error", data=error.model_dump())


__all__ = ["RunSlots", "RunsBusyError", "Slot", "research_events"]
