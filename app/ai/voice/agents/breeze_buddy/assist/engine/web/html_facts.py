"""Read one HTML page into the handful of facts later stages care about.

Deliberately a stdlib parser and nothing more. What the probe needs from a
home page is shallow — the head's metadata, where the scripts come from, the
structured-data blocks, and a few named values a page assigns to itself — and
a real-world storefront's markup is far too broken for anything strict. The
richer extraction (main text, offers, contact details) belongs to the research
stage, which reads many pages and can afford a heavier parser.

Nothing here knows what any particular site looks like: the caller says which
assignment names and which text markers it wants looked up, and gets back
whichever were present.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit

_JSON_LD_TYPE = "application/ld+json"
# Inline script text is only ever searched for known markers, so a cap costs
# nothing and keeps a 5 MB bundled page out of memory twice over.
_MAX_INLINE_SCRIPT_CHARS = 400_000
_MAX_LITERAL_VALUE_CHARS = 400
_MAX_JSON_LD_BLOCKS = 20


class _HeadParser(HTMLParser):
    """Collects head metadata, script sources and inline script text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: Optional[str] = None
        self.metas: List[Tuple[str, str]] = []
        self.link_rels: List[Dict[str, str]] = []
        self.script_srcs: List[str] = []
        self.inline_scripts: List[str] = []
        self.json_ld_raw: List[str] = []
        self._in_title = False
        self._script_kind: Optional[str] = None
        self._inline_chars = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        values = {name.lower(): (value or "") for name, value in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = values.get("name") or values.get("property") or values.get("itemprop")
            if key:
                self.metas.append((key.strip().lower(), values.get("content", "")))
        elif tag == "link":
            rel = values.get("rel", "").strip().lower()
            href = values.get("href", "").strip()
            if rel and href:
                self.link_rels.append({"rel": rel, "href": href})
        elif tag == "script":
            source = values.get("src", "").strip()
            if source:
                self.script_srcs.append(source)
                self._script_kind = None
            else:
                kind = values.get("type", "").strip().lower()
                self._script_kind = "json_ld" if kind == _JSON_LD_TYPE else "inline"

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._script_kind = None

    def handle_data(self, data: str) -> None:
        if self._in_title and self.title is None:
            text = data.strip()
            if text:
                self.title = text
        elif self._script_kind == "json_ld":
            if len(self.json_ld_raw) < _MAX_JSON_LD_BLOCKS:
                self.json_ld_raw.append(data)
        elif self._script_kind == "inline":
            if self._inline_chars < _MAX_INLINE_SCRIPT_CHARS:
                self.inline_scripts.append(data)
                self._inline_chars += len(data)


class PageFacts:
    """What one page said about itself."""

    def __init__(self, base_url: str, html: str) -> None:
        parser = _HeadParser()
        # A malformed page is the normal case, not an error: keep whatever was
        # parsed before the markup went wrong.
        try:
            parser.feed(html)
            parser.close()
        except Exception:  # noqa: BLE001 - broken markup must not fail a probe
            pass
        self.base_url = base_url
        self.title = parser.title
        self.meta: Dict[str, str] = {}
        for key, value in parser.metas:
            self.meta.setdefault(key, value.strip())
        self.link_rels = parser.link_rels
        self.script_srcs = parser.script_srcs
        self._inline = "\n".join(parser.inline_scripts)
        self._inline_lower: Optional[str] = None
        self.json_ld = _parse_json_ld(parser.json_ld_raw)

    @property
    def description(self) -> Optional[str]:
        return self.meta.get("description") or self.meta.get("og:description") or None

    @property
    def site_name(self) -> Optional[str]:
        return self.meta.get("og:site_name") or self.title

    def canonical_url(self) -> Optional[str]:
        for link in self.link_rels:
            if link["rel"] == "canonical":
                return urljoin(self.base_url, link["href"])
        return self.meta.get("og:url") or None

    def script_hosts(self) -> Dict[str, int]:
        """External script hosts and how often each appears.

        Protocol-relative and absolute sources both resolve against the page,
        so a first-party bundle counts as the site's own host.
        """
        hosts: Dict[str, int] = {}
        for source in self.script_srcs:
            host = (urlsplit(urljoin(self.base_url, source)).hostname or "").lower()
            if host:
                hosts[host] = hosts.get(host, 0) + 1
        return hosts

    def literals(self, names: Sequence[str]) -> Dict[str, str]:
        """Values a page assigns to the given names in its inline scripts.

        ``Some.name = "value"`` yields the string; an object or array yields
        its source text, which is enough to prove the assignment happened.
        """
        found: Dict[str, str] = {}
        for name in names:
            # Every quantifier is bounded. The value is only ever read for its
            # first few hundred characters, and an unbounded lazy scan over a
            # hostile 400 KB script is a denial-of-service waiting to happen.
            match = re.search(
                re.escape(name)
                + r"""\s{0,20}=\s{0,20}(?:(["'])(.{0,%d}?)\1|(\{.{0,%d}?\}|\[.{0,%d}?\]))"""
                % (
                    _MAX_LITERAL_VALUE_CHARS,
                    _MAX_LITERAL_VALUE_CHARS,
                    _MAX_LITERAL_VALUE_CHARS,
                ),
                self._inline,
                re.DOTALL,
            )
            if not match:
                continue
            value = match.group(2) if match.group(2) is not None else match.group(3)
            found[name] = (value or "").strip()[:_MAX_LITERAL_VALUE_CHARS]
        return found

    def markers(self, patterns: Sequence[str]) -> List[str]:
        """Which of ``patterns`` appear in the page's inline script text."""
        if self._inline_lower is None:
            self._inline_lower = self._inline.lower()
        haystack = self._inline_lower
        return [pattern for pattern in patterns if pattern.lower() in haystack]


def _parse_json_ld(blocks: Sequence[str]) -> List[Dict[str, Any]]:
    parsed: List[Dict[str, Any]] = []
    for block in blocks:
        try:
            value = json.loads(block)
        except (ValueError, TypeError):
            continue
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict):
                parsed.append(item)
    return parsed


__all__ = ["PageFacts"]
