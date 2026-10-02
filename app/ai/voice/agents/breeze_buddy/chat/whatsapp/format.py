"""Buddy's reply as WhatsApp text (D32: text only).

WhatsApp reads its own light markup — *bold*, _italic_, ~strike~, ```code```
— and nothing else, so the model's markdown is translated, not sent raw:
headings become bold lines, links become "text (url)", and a reply longer
than Meta's text ceiling is split on paragraph, then line, then word.
"""

import re
from typing import List

#: Meta's ceiling for one text message.
TEXT_MAX = 4096

_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.S)
_STRIKE = re.compile(r"~~(.+?)~~", re.S)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BULLET = re.compile(r"^([ \t]*)[*+][ \t]+", re.M)
_BLANKS = re.compile(r"\n{3,}")


def to_whatsapp(markdown: str) -> str:
    """PURE: markdown -> WhatsApp markup."""
    text = markdown.strip()
    text = _HEADING.sub(lambda m: f"*{m.group(1).strip('*')}*", text)
    text = _BOLD.sub(lambda m: f"*{m.group(1) or m.group(2)}*", text)
    text = _STRIKE.sub(lambda m: f"~{m.group(1)}~", text)
    text = _LINK.sub(
        lambda m: (
            m.group(2) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})"
        ),
        text,
    )
    text = _BULLET.sub(lambda m: f"{m.group(1)}- ", text)
    return _BLANKS.sub("\n\n", text)


def split(text: str, limit: int = TEXT_MAX) -> List[str]:
    """PURE: the text in parts of at most ``limit`` characters, cut at the
    last paragraph, line or word break that fits (a hard cut only for one
    unbroken run longer than the limit)."""
    parts: List[str] = []
    rest = text.strip()
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts
