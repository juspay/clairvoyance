"""Research: read a store's site for the facts an assistant is built from.

The home page is read first, whole, with its links. Then up to one page each
for about, contact, FAQ, shipping and returns is picked, from the home page's
own links before Firecrawl's list of the site's addresses, and each is read
once with the schema in ``prompts.py``. No model loop: a run costs one map
call and at most six page reads.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, List, Tuple
from urllib.parse import urlsplit

from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingUnavailableError,
    WebsiteScrapingUpstreamError,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.research.providers import (
    firecrawl,
)
from app.ai.voice.agents.breeze_buddy.assist.onboarding.research import utils
from app.ai.voice.agents.breeze_buddy.assist.onboarding.research.prompts import (
    PROMPT,
    SCHEMA,
)
from app.core.logger import logger
from app.schemas.breeze_buddy.assist.onboarding.research import (
    AssistResearchCompletion,
    AssistResearchNote,
)

# Wall-clock budget for one run.
_MAX_SECONDS = 120.0
# Longest Firecrawl may take to read one page, or to list the site (which runs
# beside the home page read, so it can take as long without slowing the run).
_PAGE_TIMEOUT_SECONDS = 60.0
# Pages read at once, within Firecrawl's concurrency.
_MAX_PARALLEL_READS = 3
OnEvent = Callable[[str, Dict[str, Any]], Awaitable[None]]


async def read_facts(url: str, *, on_event: OnEvent) -> AssistResearchCompletion:
    """Read the site at ``url`` (an already normalized https address).

    ``on_event`` receives ``("progress", {"detail"})`` as each step starts and
    ``("note", {field, value, source_url})`` for each fact as it is found.
    Raises ``WebsiteScrapingConfigurationError`` without a key, and
    ``WebsiteScrapingUpstreamError`` when no page could be read.
    """
    deadline = time.monotonic() + _MAX_SECONDS
    result = AssistResearchCompletion()
    pages_read = 0
    kept: Dict[str, int] = {}
    seen: set[Tuple[str, Tuple[str, ...]]] = set()

    async def keep(notes: List[AssistResearchNote]) -> None:
        for note in notes:
            await on_event("note", note.model_dump())

    await on_event("progress", {"detail": "Reading the home page"})
    site_map = asyncio.create_task(
        firecrawl.list_pages(url, timeout_seconds=_PAGE_TIMEOUT_SECONDS)
    )
    home_links: List[str] = []
    link_contacts: Dict[str, List[str]] = {}
    # Where the home page landed: a store may move to another domain.
    site_url = url
    try:
        source, filled, home_links = await firecrawl.read_page(
            url,
            schema=SCHEMA,
            prompt=PROMPT,
            timeout_seconds=_PAGE_TIMEOUT_SECONDS,
            whole_page=True,
        )
        pages_read += 1
        site_url = source
        link_contacts = utils.contacts_in_links(home_links)
        await keep(utils.to_notes(filled, source, kept, seen))
    except WebsiteScrapingUnavailableError:
        # Out of credits, busy or down: every other page would fail the same.
        site_map.cancel()
        raise
    except WebsiteScrapingUpstreamError as exc:
        logger.info(f"assist research: the home page could not be read: {exc}")
    except BaseException:
        site_map.cancel()
        raise
    try:
        map_links = await site_map
    except WebsiteScrapingUpstreamError as exc:
        # The home page's own links still name the key pages.
        logger.info(f"assist research: no site map: {exc}")
        map_links = []
    pages = utils.pick_pages(site_url, home_links, map_links)

    await on_event("progress", {"detail": f"Reading {len(pages)} more pages"})
    gate = asyncio.Semaphore(_MAX_PARALLEL_READS)

    async def read(page: str) -> Tuple[str, Dict[str, Any]]:
        async with gate:
            source, filled, _ = await firecrawl.read_page(
                page,
                schema=SCHEMA,
                prompt=PROMPT,
                timeout_seconds=_PAGE_TIMEOUT_SECONDS,
            )
        return source, filled

    contact_page = utils.contact_page(pages)
    tasks = [asyncio.create_task(read(page)) for page in pages]
    try:
        # Facts are kept in page order, not the order reads finish: the first
        # wording of a repeated fact wins, so the same site gives the same
        # notes. Each page still shows as soon as the pages before it are in.
        for page, task in zip(pages, tasks):
            try:
                source, filled = await asyncio.wait_for(
                    task, timeout=max(deadline - time.monotonic(), 0)
                )
            except TimeoutError:
                # Out of time: a page already read still counts.
                result.status = "timed_out"
                continue
            except WebsiteScrapingUpstreamError as exc:
                logger.info(f"assist research: a page could not be read: {exc}")
                continue
            pages_read += 1
            if page == contact_page and urlsplit(source).path.strip("/"):
                # The contact page that was read, for the "Contact us" button;
                # one that sent the reader back to the home page is none.
                await keep(
                    [
                        AssistResearchNote(
                            field="help_links",
                            value=f"Contact page: {source}",
                            source_url=source,
                        )
                    ]
                )
            await keep(utils.to_notes(filled, source, kept, seen))
    finally:
        for task in tasks:
            task.cancel()

    # A contact that is only a link (a chat icon) counts only where the pages
    # state none: a footer link can be an agency's, the stated one is the
    # store's.
    unstated = {
        name: found for name, found in link_contacts.items() if not kept.get(name)
    }
    await keep(utils.to_notes(unstated, site_url, kept, seen))

    if pages_read == 0 and result.status == "completed":
        raise WebsiteScrapingUpstreamError("no page could be read")
    return result


__all__ = ["read_facts"]
