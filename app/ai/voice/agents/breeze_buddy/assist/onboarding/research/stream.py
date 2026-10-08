"""The console's store research: one run as a stream of events."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator, Dict, Optional

from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingConfigurationError,
    WebsiteScrapingUnavailableError,
    WebsiteScrapingUpstreamError,
)
from app.ai.voice.agents.breeze_buddy.assist.onboarding.research import service
from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent
from app.core.logger import logger
from app.schemas.breeze_buddy.assist.onboarding.research import AssistResearchError

# Seconds a stream may go quiet before a ping is sent; proxies close an idle
# stream, and rendering one page can take longer than this.
PING_SECONDS = 15.0


async def research_events(url: str) -> AsyncGenerator[SSEEvent, None]:
    """Research ``url``: ``progress``, ``note`` and ``ping`` events, then
    exactly one of ``done`` or ``error``. Closing the stream early cancels the
    run."""
    queue: "asyncio.Queue[Optional[SSEEvent]]" = asyncio.Queue()
    notes_sent = 0

    async def on_event(kind: str, data: Dict[str, Any]) -> None:
        nonlocal notes_sent
        if kind == "progress":
            await queue.put(_progress(data["detail"]))
        elif kind == "note":
            notes_sent += 1
            await queue.put(SSEEvent(event="note", data=data))

    async def run() -> None:
        try:
            done = await service.read_facts(url, on_event=on_event)
            logger.info(
                "assist research complete",
                status=done.status,
                notes=notes_sent,
            )
            await queue.put(SSEEvent(event="done", data=done.model_dump()))
        except WebsiteScrapingUnavailableError as exc:
            logger.warning(f"assist research: provider unavailable: {exc}")
            await queue.put(
                _error("Research is not available right now.", retryable=True)
            )
        except WebsiteScrapingUpstreamError as exc:
            logger.info(f"assist research could not read the site: {exc}")
            await queue.put(_error("We could not read that website."))
        except WebsiteScrapingConfigurationError as exc:
            logger.error(f"assist research unavailable: {exc}")
            await queue.put(_error("Research is not available right now."))
        except Exception:
            logger.exception("assist research failed")
            await queue.put(_error("Research could not be completed.", retryable=True))
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


def _error(message: str, *, retryable: bool = False) -> SSEEvent:
    error = AssistResearchError(message=message, retryable=retryable)
    return SSEEvent(event="error", data=error.model_dump())


__all__ = ["research_events"]
