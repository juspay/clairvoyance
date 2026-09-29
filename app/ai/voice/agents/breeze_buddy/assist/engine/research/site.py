"""Read a store's site for the facts an assistant is built from.

Firecrawl lists the site's addresses, the home page and up to one page each
for about, contact, FAQ, shipping and returns are picked, and each is read
once with a schema of the fields below. No model loop: a run costs one map
call and at most ``MAX_PAGES`` page reads.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Tuple
from urllib.parse import urlsplit

from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingUpstreamError,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.research.providers import (
    firecrawl_site,
)
from app.core.logger import logger

# Wall-clock budget for one run.
MAX_SECONDS = 120.0
# Longest Firecrawl may take to list the site, and to read one page.
MAP_TIMEOUT_SECONDS = 20.0
PAGE_TIMEOUT_SECONDS = 60.0
# Addresses asked of the map; key pages sit near the top.
MAP_LIMIT = 500
# Pages read in one run (the home page plus one per kind below).
MAX_PAGES = 6
# Pages read at once, within Firecrawl's concurrency.
MAX_PARALLEL_READS = 3
# Longest fact value kept, and facts kept per field.
MAX_VALUE_CHARS = 500
MAX_NOTES_PER_FIELD = 10

# The pages worth reading, by words their address contains.
PAGE_KINDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("about", ("about", "our-story", "story")),
    ("contact", ("contact",)),
    ("faq", ("faq", "help")),
    ("shipping", ("shipping", "delivery")),
    ("returns", ("return", "refund", "exchange")),
)

# The only fields a fact may be recorded under, and what each one means.
FIELDS: Dict[str, str] = {
    "brand_line": "Who the store is and what stands behind it, in one line.",
    "what_we_sell": "What the store sells, in its own words.",
    "hero_items": "Names of items the store features or calls best sellers.",
    "offer_items": "Offers, discounts or sales running now.",
    "trust_items": "Guarantees, certifications, years in business, awards.",
    "vocabulary": "Words the store uses for its goods, and its tone.",
    "tagline": "The store's tagline or slogan.",
    "compliance": "Legal or compliance notes shoppers must be told.",
    "whatsapp": "WhatsApp number, with country code.",
    "email": "Customer support email address.",
    "returns": "The returns, refunds or exchange policy, as stated.",
    "delivery": "Shipping and delivery times, costs and areas, as stated.",
    "faq": "A common question and its answer, as 'Q: ... A: ...'.",
}

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        name: {"type": "array", "items": {"type": "string"}, "description": text}
        for name, text in FIELDS.items()
    },
}

PROMPT = (
    "Extract only what this page itself states about the store. Leave a field "
    "empty when the page does not say it. Do not guess or add general "
    "knowledge. Each list entry is one fact, kept short."
)

OnEvent = Callable[[str, Dict[str, Any]], Awaitable[None]]
# Why a run stopped; the stream sends it to the console with the facts.
ResearchStop = Literal["finished", "out_of_time"]


@dataclass(frozen=True)
class Note:
    field: str
    value: str
    source_url: str


@dataclass
class ResearchResult:
    """What one run found, how many pages it read, and why it stopped."""

    notes: List[Note] = field(default_factory=list)
    pages_read: int = 0
    stopped_because: ResearchStop = "finished"


async def research(url: str, *, on_event: Optional[OnEvent] = None) -> ResearchResult:
    """Read the site at ``url`` (an already normalized https address).

    ``on_event`` receives ``("progress", {"detail"})`` as each step starts and
    ``("note", {field, value, source_url})`` for each fact as it is found.
    Raises ``WebsiteScrapingConfigurationError`` without a key, and
    ``WebsiteScrapingUpstreamError`` when no page could be read.
    """

    async def emit(kind: str, data: Dict[str, Any]) -> None:
        if on_event:
            await on_event(kind, data)

    deadline = time.monotonic() + MAX_SECONDS
    result = ResearchResult()

    await emit("progress", {"detail": "Finding the store's pages"})
    try:
        links = await firecrawl_site.map_site(
            url, limit=MAP_LIMIT, timeout_seconds=MAP_TIMEOUT_SECONDS
        )
    except WebsiteScrapingUpstreamError as exc:
        # The home page alone still says a lot.
        logger.info(f"assist research: no site map, reading the home page: {exc}")
        links = []
    pages = pick_pages(url, links)

    await emit("progress", {"detail": f"Reading {len(pages)} pages"})
    gate = asyncio.Semaphore(MAX_PARALLEL_READS)

    async def read(page: str) -> Tuple[str, str, Dict[str, Any]]:
        async with gate:
            source, filled = await firecrawl_site.scrape_json(
                page,
                schema=SCHEMA,
                prompt=PROMPT,
                timeout_seconds=PAGE_TIMEOUT_SECONDS,
            )
        return page, source, filled

    tasks = [asyncio.create_task(read(page)) for page in pages]
    kept: Dict[str, int] = {}
    seen: set[Tuple[str, str]] = set()
    try:
        for next_read in asyncio.as_completed(
            tasks, timeout=max(deadline - time.monotonic(), 0)
        ):
            try:
                page, source, filled = await next_read
            except WebsiteScrapingUpstreamError as exc:
                logger.info(f"assist research: a page could not be read: {exc}")
                continue
            result.pages_read += 1
            # The home page may move to another domain; a page linked from it
            # that lands off the site is someone else's words.
            if page != url and not same_site(url, source):
                continue
            for note in _notes(filled, source, kept, seen):
                result.notes.append(note)
                await emit("note", note.__dict__)
    except TimeoutError:
        result.stopped_because = "out_of_time"
    finally:
        for task in tasks:
            task.cancel()

    if result.pages_read == 0 and result.stopped_because == "finished":
        raise WebsiteScrapingUpstreamError("no page of the site could be read")
    return result


def pick_pages(home: str, links: List[str]) -> List[str]:
    """The home page, then the first on-site address for each kind of page."""
    pages = [home]
    candidates = sorted(
        {
            link
            for link in links
            if link.startswith("https://") and same_site(home, link)
        },
        key=lambda link: (len(urlsplit(link).path), link),
    )
    for _, words in PAGE_KINDS:
        for link in candidates:
            path = urlsplit(link).path.lower()
            if link not in pages and any(word in path for word in words):
                pages.append(link)
                break
    return pages[:MAX_PAGES]


def same_site(home: str, other: str) -> bool:
    """``other`` is on ``home``'s host or one of its subdomains (``www.``
    ignored)."""
    root = _host(home)
    host = _host(other)
    return bool(root) and (host == root or host.endswith("." + root))


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _notes(
    filled: Dict[str, Any],
    source: str,
    kept: Dict[str, int],
    seen: set[Tuple[str, str]],
) -> List[Note]:
    notes: List[Note] = []
    for name in FIELDS:
        values = filled.get(name)
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, str):
                continue
            text = " ".join(value.split())[:MAX_VALUE_CHARS]
            # Pages repeat a fact with other punctuation ("policy" / "policy.").
            key = (name, "".join(ch for ch in text.lower() if ch.isalnum()))
            if not text or key in seen or kept.get(name, 0) >= MAX_NOTES_PER_FIELD:
                continue
            seen.add(key)
            kept[name] = kept.get(name, 0) + 1
            notes.append(Note(field=name, value=text, source_url=source))
    return notes


__all__ = ["FIELDS", "Note", "ResearchResult", "pick_pages", "research", "same_site"]
