"""The researcher's tools: read a merchant's pages and pull facts out of them.

``read_pages`` fetches, ``page_links`` finds where to go next and the contact
details on a page, ``find_text`` searches what was read, and ``Evidence``
records every page read and every fact noted with the page it came from.

Page text is attacker-controlled and the model chooses what to read, so reads
stay on the merchant's own host (every redirect hop is checked before it is
sent) and within a budget, the model searches by plain phrase rather than
regex, and CPU-bound helpers run off the event loop.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from itertools import islice
from typing import Dict, Iterable, List, Sequence, Set
from urllib.parse import urljoin, urlsplit

from app.ai.voice.agents.breeze_buddy.assist.engine.research.config import (
    MAX_AT_SIGNS,
    MAX_LINKS_PER_PAGE,
    MAX_PAGE_BYTES,
    MAX_PARALLEL_READS,
    MAX_PASSAGES,
    MAX_PHRASE_LENGTH,
    MAX_PHRASE_OCCURRENCES,
    MAX_READS_PER_CALL,
    MAX_READS_PER_RUN,
    MAX_TEXT_PER_RUN,
    MIN_PHRASE_LENGTH,
    PASSAGE_CHARS,
    READ_TIMEOUT_SECONDS,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    MAX_URL_LENGTH,
    EgressNotGuardedError,
    FetchFailedError,
    FetchResult,
    UnsafeUrlError,
    fetch_page,
)
from app.core.logger import logger

# Longest part of an email before the "@".
_EMAIL_LOCAL_MAX = 64
# Characters allowed before the "@".
_EMAIL_LOCAL_CLASS = "A-Za-z0-9._%+-"
# Files, not pages or mail domains: "icon@2x.png" looks like an address.
_FILE_ENDINGS = frozenset(
    ("js", "mjs", "css", "png", "jpg", "jpeg", "webp", "avif", "gif", "svg")
    + ("ico", "woff", "woff2", "ttf", "mp4", "webm", "pdf", "zip")
)
_FILE_SUFFIXES = tuple(f".{ending}" for ending in _FILE_ENDINGS)
# Content types kept as text; anything else (images, fonts) is dropped.
_TEXT_CONTENT_TYPES = ("text/", "json", "xml")

# Each pattern stops at a delimiter or a fixed length, so it runs in linear time
# on hostile text.
# Link targets in href="...".
_HREF = re.compile(r"""href\s*=\s*["']([^"'>\s]+)["']""", re.IGNORECASE)
# Phone numbers from tel: and wa.me links.
_TEL = re.compile(r"(?:tel:|wa\.me/)\+?([0-9][0-9 ()-]{6,17})", re.IGNORECASE)
# Emails are matched outward from each "@": trying a pattern at every position
# is quadratic on hostile text.
_EMAIL_LOCAL_CHAR = re.compile(f"[{_EMAIL_LOCAL_CLASS}]")
_EMAIL_LOCAL = re.compile(f"[{_EMAIL_LOCAL_CLASS}]{{1,{_EMAIL_LOCAL_MAX}}}\\Z")
_EMAIL_DOMAIN = re.compile(
    r"[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,6}\.[A-Za-z]{2,24}"
)


@dataclass
class Page:
    """One URL that was read, and how it came back."""

    url: str
    status: int
    text: str
    size_bytes: int
    truncated: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.status < 400 and bool(self.text)


@dataclass
class Snippet:
    """Text found on a page, and the page it was found on."""

    text: str
    source_url: str


@dataclass
class Note:
    """A fact the researcher recorded, and the page that said it."""

    field_name: str
    value: str
    source_url: str


@dataclass
class PageLinks:
    """Where a page points: other pages, emails and phones."""

    pages: List[str] = field(default_factory=list)
    emails: List[str] = field(default_factory=list)
    phones: List[str] = field(default_factory=list)


@dataclass
class Evidence:
    """One research run: the pages read, the facts noted, the reads spent."""

    root: str
    pages: Dict[str, Page] = field(default_factory=dict)
    notes: List[Note] = field(default_factory=list)
    reads: int = 0
    kept_chars: int = 0
    # Every URL asked for or landed on, so a redirect is not read twice.
    requested: Set[str] = field(default_factory=set)

    def add(self, page: Page) -> None:
        # Two reads can land on the same URL; the later one replaces the first.
        replaced = self.pages.get(page.url)
        if replaced is not None:
            self.kept_chars -= len(replaced.text)
        room = max(0, MAX_TEXT_PER_RUN - self.kept_chars)
        if len(page.text) > room:
            page.text, page.truncated = page.text[:room], True
        self.kept_chars += len(page.text)
        self.pages[page.url] = page
        self.requested.add(page.url)

    def note(self, field_name: str, value: str, source_url: str) -> None:
        text = (value or "").strip()
        if text:
            self.notes.append(
                Note(field_name=field_name, value=text, source_url=source_url)
            )

    def readable(self) -> List[Page]:
        return [page for page in self.pages.values() if page.ok]

    def seen(self, url: str) -> bool:
        return url in self.requested

    def claim(self, url: str) -> None:
        self.requested.add(url)
        self.reads += 1

    def release(self, url: str) -> None:
        # A page another read already landed on stays read.
        if url in self.requested and url not in self.pages:
            self.requested.discard(url)
            self.reads -= 1


def site_of(url: str) -> str:
    """The URL's host, lowercased and without ``www.``; "" if it cannot parse."""
    if len(url) > MAX_URL_LENGTH:
        return ""
    try:
        host = urlsplit(url if "//" in url else f"https://{url}").hostname or ""
    except ValueError:
        return ""
    host = host.lower().strip(".")
    if not host.isascii():
        # Fetched URLs carry the ASCII (xn--) form; compare like with like.
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return ""
    return host[4:] if host.startswith("www.") else host


def same_site(url: str, root: str) -> bool:
    """Whether ``url`` is on ``root``'s host or a subdomain of it.

    The whole host is compared: matching only the last two labels would make
    every store on a shared hosting domain one site.
    """
    host, base = site_of(url), site_of(root)
    return bool(host and base) and (host == base or host.endswith("." + base))


async def read_pages(urls: Sequence[str], evidence: Evidence) -> List[Page]:
    """Read on-site URLs not read before, within the run's budget.

    A failed read comes back as a ``Page`` with ``error`` set, never raised;
    only ``EgressNotGuardedError`` propagates, since every read would fail.
    """
    budget = min(MAX_READS_PER_CALL, MAX_READS_PER_RUN - evidence.reads)
    if budget <= 0:
        return []
    wanted = _pick(urls, evidence, budget)
    if not wanted:
        return []

    # Claimed before the first await, so concurrent calls cannot overspend.
    for url in wanted:
        evidence.claim(url)

    gate = asyncio.Semaphore(MAX_PARALLEL_READS)

    async def read_one(url: str) -> Page:
        async with gate:
            page = await _read(url, evidence.root)
            # Kept as it lands, so the run's text cap bounds memory too.
            evidence.add(page)
            return page

    tasks = [asyncio.ensure_future(read_one(url)) for url in wanted]
    try:
        pages = await asyncio.gather(*tasks)
    except BaseException:
        # Finished reads are kept, even when they landed on another URL; the
        # rest are stopped and handed back to the budget.
        for url, task in zip(wanted, tasks):
            if task.done() and not task.cancelled():
                continue
            task.cancel()
            evidence.release(url)
        raise
    return list(pages)


def _pick(urls: Sequence[str], evidence: Evidence, budget: int) -> List[str]:
    """The first ``budget`` URLs that are new and on the merchant's site."""
    wanted: List[str] = []
    for raw in urls:
        url = (raw or "").strip()
        if not url or evidence.seen(url) or url in wanted:
            continue
        # fetch_page refuses anything else, after the read is paid for.
        if "://" in url and not url.startswith("https://"):
            continue
        if not same_site(url, evidence.root):
            continue
        wanted.append(url)
        if len(wanted) >= budget:
            break
    return wanted


async def _read(url: str, root: str) -> Page:
    try:
        result = await fetch_page(
            url,
            max_bytes=MAX_PAGE_BYTES,
            timeout_seconds=READ_TIMEOUT_SECONDS,
            allow_url=lambda hop: same_site(hop, root),
        )
    except EgressNotGuardedError:
        raise
    except (UnsafeUrlError, FetchFailedError) as exc:
        return _failed(url, str(exc))
    except Exception as exc:
        logger.warning(f"assist research: {url} unreadable ({exc})")
        return _failed(url, "unreadable")
    return _as_page(url, result)


def _failed(url: str, error: str) -> Page:
    return Page(url=url, status=0, text="", size_bytes=0, error=error)


def _as_page(url: str, result: FetchResult) -> Page:
    content_type = (result.headers.get("content-type") or "").lower()
    is_text = not content_type or any(
        marker in content_type for marker in _TEXT_CONTENT_TYPES
    )
    return Page(
        url=result.final_url or url,
        status=result.status,
        text=result.body if is_text else "",
        size_bytes=result.size_bytes,
        truncated=result.truncated,
    )


async def find_text(
    phrase: str, pages: Iterable[Page], *, limit: int = MAX_PASSAGES
) -> List[Snippet]:
    """Every passage containing ``phrase``, case- and whitespace-insensitive."""
    return await asyncio.to_thread(_find_text, phrase, list(pages), limit)


def _find_text(phrase: str, pages: List[Page], limit: int) -> List[Snippet]:
    needle = " ".join((phrase or "").split()).lower()
    if not MIN_PHRASE_LENGTH <= len(needle) <= MAX_PHRASE_LENGTH:
        return []
    found: List[Snippet] = []
    seen: Set[str] = set()
    occurrences = 0
    for page in pages:
        flat = " ".join(page.text.split())
        haystack = flat.lower()
        # Lowercasing can change a string's length; offsets then only hold
        # for the lowered copy.
        source = flat if len(haystack) == len(flat) else haystack
        at = haystack.find(needle)
        while at != -1:
            occurrences += 1
            if occurrences > MAX_PHRASE_OCCURRENCES:
                return found
            start = max(0, at - PASSAGE_CHARS)
            passage = source[start : at + len(needle) + PASSAGE_CHARS].strip()
            if passage not in seen:
                seen.add(passage)
                found.append(Snippet(text=passage, source_url=page.url))
                if len(found) >= limit:
                    return found
            at = haystack.find(needle, at + len(needle))
    return found


async def page_links(page: Page) -> PageLinks:
    """Links to other pages, emails and phone numbers on ``page``."""
    return await asyncio.to_thread(_page_links, page)


def _page_links(page: Page) -> PageLinks:
    links = PageLinks()
    for raw in _first(_HREF, page.text, MAX_LINKS_PER_PAGE):
        if raw.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        try:
            target = urljoin(page.url, raw)
            path = urlsplit(target).path.lower()
        except ValueError:
            continue
        if not path.endswith(_FILE_SUFFIXES):
            links.pages.append(target)
    links.pages = list(dict.fromkeys(links.pages))
    links.emails = _emails(page.text)
    links.phones = list(dict.fromkeys(_first(_TEL, page.text, MAX_LINKS_PER_PAGE)))
    return links


def _first(pattern: re.Pattern[str], text: str, limit: int) -> List[str]:
    return [match.group(1) for match in islice(pattern.finditer(text), limit)]


def _emails(text: str) -> List[str]:
    found: List[str] = []
    at = text.find("@")
    checked = 0
    while at != -1 and checked < MAX_AT_SIGNS:
        # CSS (@media) and JS (@import) put "@" after a space or brace; only
        # an "@" that could end a local part counts towards the cap.
        if at > 0 and _EMAIL_LOCAL_CHAR.match(text, at - 1):
            checked += 1
            local = _EMAIL_LOCAL.search(text, max(0, at - _EMAIL_LOCAL_MAX), at)
            domain = _EMAIL_DOMAIN.match(text, at + 1)
            if local and domain:
                ending = domain.group(0).rsplit(".", 1)[-1].lower()
                if ending not in _FILE_ENDINGS:
                    found.append(f"{local.group(0)}@{domain.group(0)}")
        at = text.find("@", at + 1)
    return list(dict.fromkeys(found))


__all__ = [
    "Evidence",
    "Note",
    "Page",
    "PageLinks",
    "Snippet",
    "find_text",
    "page_links",
    "read_pages",
    "same_site",
]
