"""Buddy's reply as WhatsApp text (D32: text only).

WhatsApp reads its own light markup — *bold*, _italic_, ~strike~, ```code```
— and nothing else, so the model's markdown is translated, not sent raw:
headings become bold lines and links become "text (url)".
"""

import re

#: The markup line the prompt adds on WhatsApp (chat/agent/context.py).
MARKUP_HINT = "Use *bold* sparingly and plain links."

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
