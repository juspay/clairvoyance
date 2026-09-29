"""Research helpers: which pages of a site to read, and Firecrawl's answers
turned into notes."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from app.ai.voice.agents.breeze_buddy.assist.onboarding.research.prompts import (
    FIELDS,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms import registry
from app.schemas.breeze_buddy.assist.onboarding.research import AssistResearchNote

# Longest fact value kept, and facts kept per field.
_MAX_VALUE_CHARS = 500
_MAX_NOTES_PER_FIELD = 12

# The pages worth reading, by words their address contains.
_PAGE_KINDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("about", ("about", "our-story", "story")),
    ("contact", ("contact",)),
    ("faq", ("faq", "help")),
    ("shipping", ("shipping", "delivery")),
    ("returns", ("return", "refund", "exchange")),
)
# File endings that are never a page: documents, scripts, styles, images.
_FILE_ENDINGS = (
    ".pdf",
    ".js",
    ".css",
    ".json",
    ".xml",
    ".txt",
    ".csv",
    ".zip",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".mp4",
    ".woff",
    ".woff2",
)
# Folders that never hold a page about the store.
_SKIPPED_FOLDERS = frozenset({"apps", "blog", "blogs", "brand", "brands", "tags"})


def pick_pages(home: str, home_links: List[str], map_links: List[str]) -> List[str]:
    """The best on-site address for each kind of page: one the home page links
    to before one only the map lists, then the shortest."""
    # One address per page: "/pages/faq/" and "/pages/faq#top" are "/pages/faq".
    unique: Dict[str, str] = {}
    for link in home_links + map_links:
        parts = urlsplit(link)
        unique.setdefault(
            f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}?{parts.query}",
            link,
        )
    usable = [
        link
        for link in unique.values()
        if link.startswith("https://")
        and same_site(home, link)
        and urlsplit(link).path.strip("/")
        and _is_web_page(link)
    ]
    skipped = _SKIPPED_FOLDERS | registry.folders_to_skip()
    own = set(home_links)
    candidates = sorted(
        (link for link in usable if not _folders(link) & skipped),
        key=lambda link: (link not in own, len(urlsplit(link).path), link),
    )
    pages: List[str] = []
    for _, words in _PAGE_KINDS:
        for link in candidates:
            path = urlsplit(link).path.lower()
            if link not in pages and any(word in path for word in words):
                pages.append(link)
                break
    return pages


def _is_web_page(link: str) -> bool:
    """A page, not a file: "Annual Return 2022.pdf" is no returns policy and
    "_commonjsHelpers.js" no help page; "returns.php" is a page."""
    return not urlsplit(link).path.lower().rstrip("/").endswith(_FILE_ENDINGS)


def _folders(link: str) -> set[str]:
    """Every folder in a link's path: "/en-in/items/pouch" is in "en-in" and
    "items" (a store may put its pages behind a language folder)."""
    return set(urlsplit(link).path.lower().strip("/").split("/")[:-1])


def contacts_in_links(links: List[str]) -> Dict[str, List[str]]:
    """WhatsApp numbers and emails a page names only as links."""
    found: Dict[str, List[str]] = {"whatsapp": [], "email": []}
    for link in links:
        parts = urlsplit(link)
        host = (parts.hostname or "").lower()
        if parts.scheme == "mailto":
            found["email"].append(unquote(parts.path))
            continue
        number = ""
        if host == "wa.me":
            number = parts.path.strip("/")
        elif host.endswith("whatsapp.com"):
            number = (parse_qs(parts.query).get("phone") or [""])[0]
        digits = "".join(ch for ch in number if ch.isdigit())
        if len(digits) >= 8:
            found["whatsapp"].append(f"+{digits}")
    return found


def same_site(home: str, other: str) -> bool:
    """``other`` is on ``home``'s host or one of its subdomains (``www.``
    ignored)."""
    root, host = (
        (urlsplit(url).hostname or "").lower().removeprefix("www.")
        for url in (home, other)
    )
    return bool(root) and (host == root or host.endswith("." + root))


def to_notes(
    filled: Dict[str, Any],
    source: str,
    kept: Dict[str, int],
    seen: set[Tuple[str, str]],
) -> List[AssistResearchNote]:
    notes: List[AssistResearchNote] = []
    for name in FIELDS:
        values = filled.get(name)
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, str):
                continue
            text = " ".join(value.split())[:_MAX_VALUE_CHARS]
            # Pages repeat a fact with other punctuation ("policy" / "policy.").
            key = (name, "".join(ch for ch in text.lower() if ch.isalnum()))
            # No letter or digit ("...", "-") is no fact.
            if not key[1] or key in seen or kept.get(name, 0) >= _MAX_NOTES_PER_FIELD:
                continue
            seen.add(key)
            kept[name] = kept.get(name, 0) + 1
            notes.append(AssistResearchNote(field=name, value=text, source_url=source))
    return notes


__all__ = ["contacts_in_links", "pick_pages", "same_site", "to_notes"]
