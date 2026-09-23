"""The four things a researcher needs, and a book to write findings in.

Why these four and not a pipeline: a pipeline runs a fixed ladder and reports
failure at the bottom. Measured on a live site 2026-09-10 — a client-routed app
whose home page, framework data endpoint and eight conventional paths all
returned the same 16 KB shell, byte for byte — the ladder finds nothing at all.
What worked was noticing that every attempt had failed *the same way*, and
concluding the text must be in the script bundles: read the shell for its module
manifest, pull the sixty files it names, mine them for sentences. That produced
the brand's positioning, its whole partner list and its support addresses.

The judgement in that paragraph is the researcher's, not this module's. These
are only the hands:

    ``gather``   fetch many URLs at once, cheaply and safely
    ``mine``     find a pattern across everything gathered so far
    ``trails``   the links, script sources, asset paths and addresses in a page
    ``Evidence`` what was learnt, and the page that said so

Every finding carries a URL. A fact nobody can trace is a fact nobody can argue
with, and this whole surface exists to be argued with by the merchant before it
becomes their assistant.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit

from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    EgressNotGuardedError,
    FetchFailedError,
    FetchResult,
    UnsafeUrlError,
    fetch_page,
)
from app.core.logger import logger

# A researcher reads text, including text a browser would have executed. Script
# bundles are ordinary text and are where a client-rendered site keeps its
# words, so they are in scope — capped, because a bundle is not a page.
DEFAULT_DOC_BYTES = 2 * 1024 * 1024
DEFAULT_GATHER_TIMEOUT = 20.0
# Wide enough to make a sixty-file sweep one step rather than sixty, narrow
# enough that we are never mistaken for a load test.
MAX_PARALLEL = 8
MAX_PER_GATHER = 80

_TEXTUAL = ("text/", "javascript", "json", "xml", "ecmascript")


@dataclass
class Doc:
    """One thing that was read, and how it came back."""

    url: str
    status: int
    content_type: str
    text: str
    size_bytes: int
    truncated: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.status < 400 and bool(self.text)

    def fingerprint(self) -> Tuple[int, int]:
        """Size and status — two pages sharing both are usually one page.

        This is what turns eight identical failures into a single fact worth
        acting on, which is the observation the whole loop turns on.
        """
        return (self.size_bytes, self.status)


@dataclass
class Hit:
    """A pattern match, and the document it came out of."""

    value: str
    source_url: str


@dataclass
class Note:
    """One thing learnt, and where it was read."""

    field_name: str
    value: str
    source_url: str
    on_site: bool = True
    noted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Evidence:
    """Everything gathered, and everything concluded from it.

    Kept apart on purpose. ``docs`` is what the site said; ``notes`` is what we
    claim it means. Only notes reach a prompt, and every note names a document.
    """

    root: str
    docs: Dict[str, Doc] = field(default_factory=dict)
    notes: List[Note] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)
    fetches: int = 0

    def add(self, doc: Doc) -> None:
        self.docs[doc.url] = doc
        self.fetches += 1

    def note(
        self, field_name: str, value: str, source_url: str, *, on_site: bool = True
    ) -> None:
        text = (value or "").strip()
        if not text:
            return
        self.notes.append(
            Note(
                field_name=field_name,
                value=text,
                source_url=source_url,
                on_site=on_site,
            )
        )

    def step(self, description: str) -> None:
        """What was tried, in order. The trace is the reviewable artefact."""
        self.steps.append(description)

    def readable(self) -> List[Doc]:
        return [doc for doc in self.docs.values() if doc.ok]

    def text_bytes(self) -> int:
        return sum(len(doc.text) for doc in self.readable())

    def seen(self, url: str) -> bool:
        return url in self.docs


def site_of(url: str) -> str:
    """The registrable-ish site a URL belongs to.

    Deliberately crude — the last two labels. It decides only whether a URL is
    "this brand's own" for the fetch policy; a public suffix list would be more
    correct and is not worth its weight for that question.
    """
    host = urlsplit(url if "//" in url else f"https://{url}").hostname or ""
    labels = host.lower().strip(".").split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host.lower()


def same_site(url: str, root: str) -> bool:
    return bool(url) and site_of(url) == site_of(root)


async def gather(
    urls: Sequence[str],
    evidence: Evidence,
    *,
    max_bytes: int = DEFAULT_DOC_BYTES,
    timeout_seconds: float = DEFAULT_GATHER_TIMEOUT,
    allow_off_site: bool = False,
) -> List[Doc]:
    """Read many URLs at once. Failures come back as documents, not exceptions.

    A researcher learns as much from a 404 as from a page, and more from eight
    identical 200s than from any one of them, so nothing here raises.
    """
    wanted: List[str] = []
    for raw in urls:
        url = (raw or "").strip()
        if not url or evidence.seen(url) or url in wanted:
            continue
        if not allow_off_site and not same_site(url, evidence.root):
            evidence.step(f"skipped off-site {url}")
            continue
        wanted.append(url)
        if len(wanted) >= MAX_PER_GATHER:
            break

    if not wanted:
        return []

    gate = asyncio.Semaphore(MAX_PARALLEL)

    async def one(url: str) -> Doc:
        async with gate:
            try:
                result = await fetch_page(
                    url, max_bytes=max_bytes, timeout_seconds=timeout_seconds
                )
            except EgressNotGuardedError:
                # Not this URL's fault and not survivable: every fetch will
                # fail the same way, so failing softly would burn the whole
                # budget discovering that eighty times.
                raise
            except (UnsafeUrlError, FetchFailedError) as exc:
                return Doc(
                    url=url,
                    status=0,
                    content_type="",
                    text="",
                    size_bytes=0,
                    error=str(exc),
                )
            except (
                Exception
            ) as exc:  # noqa: BLE001 - a researcher never dies of one bad URL
                logger.info(f"assist research: {url} unreadable ({exc})")
                return Doc(
                    url=url,
                    status=0,
                    content_type="",
                    text="",
                    size_bytes=0,
                    error="unreadable",
                )
            return _as_doc(url, result)

    docs = await asyncio.gather(*(one(url) for url in wanted))
    for doc in docs:
        evidence.add(doc)
    read = sum(1 for doc in docs if doc.ok)
    evidence.step(f"read {len(docs)} url(s), {read} usable")
    return list(docs)


def _as_doc(url: str, result: FetchResult) -> Doc:
    content_type = (result.headers.get("content-type") or "").lower()
    textual = any(marker in content_type for marker in _TEXTUAL) or not content_type
    return Doc(
        url=result.final_url or url,
        status=result.status,
        content_type=content_type,
        text=result.body if textual else "",
        size_bytes=result.size_bytes,
        truncated=result.truncated,
    )


def mine(pattern: str, docs: Iterable[Doc], *, limit: int = 200) -> List[Hit]:
    """Every match for ``pattern`` across ``docs``, first occurrence winning.

    Group 1 is taken when the pattern has one, so a caller can say what part of
    the match is the answer without post-processing every hit.
    """
    try:
        expression = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        logger.info(f"assist research: bad pattern {pattern!r} ({exc})")
        return []

    hits: List[Hit] = []
    seen: set[str] = set()
    for doc in docs:
        if not doc.text:
            continue
        for match in expression.finditer(doc.text):
            value = (match.group(1) if expression.groups else match.group(0)).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            hits.append(Hit(value=value, source_url=doc.url))
            if len(hits) >= limit:
                return hits
    return hits


# Where a page keeps the addresses of other things. Written as raw text
# patterns rather than a parse tree because a script bundle is not a document
# and there is nothing to parse — the strings are simply in there.
_HREF = re.compile(r"""href\s*=\s*["']([^"'>\s]+)["']""", re.IGNORECASE)
_SRC = re.compile(r"""src\s*=\s*["']([^"'>\s]+)["']""", re.IGNORECASE)
_MODULE = re.compile(r"""["'`]([A-Za-z0-9_./-]*/[A-Za-z0-9_.-]+\.(?:js|mjs))["'`]""")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Things with no words in them. A researcher reading these learns nothing.
_NOT_TEXT = (
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".mp4",
    ".webm",
    ".pdf",
    ".zip",
)
_TEL = re.compile(r"(?:tel:|wa\.me/)\+?([0-9][0-9 ()-]{6,17})", re.IGNORECASE)


@dataclass
class Trails:
    """The addresses a document leaves behind."""

    pages: List[str] = field(default_factory=list)
    scripts: List[str] = field(default_factory=list)
    emails: List[str] = field(default_factory=list)
    phones: List[str] = field(default_factory=list)


def trails(doc: Doc, *, base: Optional[str] = None) -> Trails:
    """Links, script addresses, and the ways to reach a human.

    Script addresses include ones named *inside* other scripts, not only those
    the markup declares: a client-routed site names its route bundles in its
    manifest, and those bundles are where its words live.

    ``base`` defaults to the document's own URL, which is the only correct
    choice: a bundle names its siblings relatively, and resolving those against
    the site root turns sixty real files into sixty 404s.
    """
    base = base or doc.url
    found = Trails()
    if not doc.text:
        return found

    # Sorted by what a thing IS, not by the attribute that named it. A
    # client-routed page declares its entry bundles with `rel=modulepreload`,
    # so they arrive as `href` — filing those under pages loses the only
    # thread that leads to the site's actual words.
    for raw in (
        _HREF.findall(doc.text) + _SRC.findall(doc.text) + _MODULE.findall(doc.text)
    ):
        if raw.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        target = urljoin(base, raw)
        path = urlsplit(target).path.lower()
        if path.endswith((".js", ".mjs")):
            found.scripts.append(target)
        elif not path.endswith(_NOT_TEXT):
            found.pages.append(target)
    found.emails = list(dict.fromkeys(_EMAIL.findall(doc.text)))
    found.phones = list(dict.fromkeys(_TEL.findall(doc.text)))
    found.pages = list(dict.fromkeys(found.pages))
    found.scripts = list(dict.fromkeys(found.scripts))
    return found


def looks_client_routed(docs: Sequence[Doc]) -> bool:
    """Do these documents differ at all?

    Three or more readable URLs sharing one size and status is a server
    answering every address with the same shell. It is the single observation
    that says "stop asking for pages and go and read the bundles", and it is
    worth a named function because a researcher must be told to look for it.
    """
    readable = [doc for doc in docs if doc.ok]
    if len(readable) < 3:
        return False
    prints = {doc.fingerprint() for doc in readable}
    return len(prints) == 1


def readable_lines(
    docs: Iterable[Doc], *, minimum: int = 30, limit: int = 400
) -> List[Hit]:
    """Sentences a person wrote, pulled out of anything — markup or bundle.

    Quoted strings of prose length, minus the ones that are obviously machinery.
    Crude on purpose: the model reading these decides what is copy and what is
    a class name. Filtering harder here would throw away the good ones too.
    """
    quoted = (
        "[\"'`]([A-Za-z][A-Za-z0-9 ,.:;%&/()'\u2019!?\u2014-]{"
        + str(minimum)
        + ",220})[\"'`]"
    )
    # Mine far wider than we keep: the filter below throws away most of
    # what a bundle quotes, and a 3x margin ran out before the copy did.
    hits = mine(quoted, docs, limit=limit * 20)
    kept: List[Hit] = []
    for hit in hits:
        value = hit.value
        if _is_machinery(value):
            continue
        kept.append(hit)
        if len(kept) >= limit:
            break
    return kept


_MACHINERY = re.compile(
    # Path data, code, CSS declarations, and the hyphenated soup of class
    # names. Measured on a client-routed site: without the `;` and the
    # framework-scope rules, eight of the first twelve "sentences" were
    # stylesheet fragments and component classes.
    r"(^[Mm][0-9]|function|prototype|undefined|webpack|svelte-|"
    r"[a-z]+-[a-z]+-[a-z]+|;|\{|\.js$|^[a-z-]+$|px |rgba?\()",
)


def _is_machinery(value: str) -> bool:
    """Class names, path data and code, as opposed to something written."""
    if _MACHINERY.search(value):
        return True
    # Real copy is a sentence. Three words is the floor at which a short line
    # of marketing prose survives and "merchant-view toolbar" does not.
    return len(value.split()) < 3


__all__ = [
    "DEFAULT_DOC_BYTES",
    "Doc",
    "Evidence",
    "Hit",
    "MAX_PARALLEL",
    "MAX_PER_GATHER",
    "Note",
    "Trails",
    "gather",
    "looks_client_routed",
    "mine",
    "readable_lines",
    "same_site",
    "site_of",
    "trails",
]
