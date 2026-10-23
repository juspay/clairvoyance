"""Buddy's reply as a messaging channel's text (D32: text only).

The model writes markdown; each channel reads its own light markup, or
none. So a reply is RENDERED for the thread's channel (formats/<channel>.py)
and then split to fit the channel's text ceiling — its
``ConversationProfile.text_max`` — on paragraph, then line, then word. A
channel joins by adding its entry to FORMATS; one without an entry gets the
words as written.
"""

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.chat.inbox.formats import whatsapp


@dataclass(frozen=True)
class ChannelText:
    """How one channel shows Buddy's words."""

    #: markdown -> the channel's markup.
    render: Callable[[str], str]
    #: One line the prompt adds: the markup the channel reads.
    markup_hint: str


FORMATS: Dict[str, ChannelText] = {
    "whatsapp": ChannelText(
        render=whatsapp.to_whatsapp, markup_hint=whatsapp.MARKUP_HINT
    ),
}


def render(channel: str, markdown: str) -> str:
    """PURE: the reply as ``channel`` shows it."""
    entry = FORMATS.get(channel)
    return entry.render(markdown) if entry is not None else markdown.strip()


def markup_hint(channel: Optional[str]) -> Optional[str]:
    """PURE: the prompt's markup line for ``channel``, None when it has none."""
    entry = FORMATS.get(channel or "")
    return entry.markup_hint if entry is not None else None


def split(text: str, limit: int) -> List[str]:
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
