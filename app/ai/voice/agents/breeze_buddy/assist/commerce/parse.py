"""The inverse of the brand block: a live prompt read back as editable fields.

Onboarding writes these sections; the studio has to show them as fields again
so a merchant edits "trust signals" rather than a wall of Markdown. Since one
module writes the format and this one reads it, they are kept side by side —
change the assembly without changing this and the studio silently starts
showing a merchant empty fields for text that is plainly in their prompt.

Tolerant on purpose. A prompt a human has already edited by hand will not
match byte for byte, and the right failure is a field that comes back empty
for them to refill, never a parse error that hides the whole editor.
"""

from __future__ import annotations

import re
from typing import Dict, List, Mapping, Sequence, Tuple

# The studio's deciding-question slots, in order. Mirrors the vertical's
# own tuple — the module that writes the section and the module that reads
# it back are kept side by side for exactly this reason.
QUESTION_SUFFIXES = ("", "_2", "_3")

_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")
_FOUR = {
    "assistant name": "assistant_name",
    "brand": "brand_line",
    "tagline / positioning": "tagline",
}
# `- **Storefront:** `{shop_url}` (domain) | Currency: INR (₹)`
_STOREFRONT = re.compile(
    r"\*\*Storefront:\*\*[^(]*(?:\(([^)]*)\))?\s*(?:\|\s*Currency:\s*(.*))?$"
)
_HEAD_TO_FIELD = {
    "what we sell": "what_we_sell",
    "vocabulary register": "vocabulary",
    "compliance note": "compliance",
}
_HEAD_TO_LIST = {
    "trust signals": "trust_items",
    "active offers": "offer_items",
}
_WHATSAPP = re.compile(r"WhatsApp / phone\s*(.+?)\s*\*\*\s*$")
_EMAIL = re.compile(r"Email:\s*`?([^`\s]+)`?\s*$")


def _sections(text: str) -> Dict[str, List[str]]:
    """`### Heading` → its lines, plus everything before the first one."""
    out: Dict[str, List[str]] = {"": []}
    current = ""
    for line in text.splitlines():
        if line.startswith("### "):
            current = line[4:].strip().lower()
            out.setdefault(current, [])
            continue
        out[current].append(line)
    return out


def _text(lines: Sequence[str]) -> str:
    return "\n".join(lines).strip()


def _items(lines: Sequence[str]) -> List[str]:
    found: List[str] = []
    for line in lines:
        hit = _BULLET.match(line)
        if hit and hit.group(1).strip():
            found.append(hit.group(1).strip())
    return found


def _preamble(lines: Sequence[str]) -> str:
    """The prose above a section's bullets — the hero note lives here."""
    kept: List[str] = []
    for line in lines:
        if _BULLET.match(line):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def fields_from_prompt(
    prompt: str, configurations: Mapping[str, object]
) -> Dict[str, List[str]]:
    """Everything a merchant may edit, read back out of a built agent."""
    fields: Dict[str, List[str]] = {}

    def put(key: str, value: str) -> None:
        if value and value.strip():
            fields[key] = [value.strip()]

    def put_all(key: str, values: Sequence[str]) -> None:
        if values:
            fields[key] = list(values)

    start = prompt.find("## Brand identity")
    block = prompt[start:] if start >= 0 else prompt
    end = block.find("\n## ", 1)
    if end > 0:
        block = block[:end]
    parts = _sections(block)

    for line in parts.get("", []):
        hit = _BULLET.match(line)
        if not hit:
            continue
        body = hit.group(1)
        storefront = _STOREFRONT.search(body)
        if storefront:
            put("domain", (storefront.group(1) or "").strip())
            put("currency", (storefront.group(2) or "").strip())
            continue
        label = re.match(r"\*\*(.+?):\*\*\s*(.*)$", body)
        if label:
            key = _FOUR.get(label.group(1).strip().lower())
            if key:
                put(key, label.group(2))

    for heading, key in _HEAD_TO_FIELD.items():
        put(key, _text(parts.get(heading, [])))
    for heading, key in _HEAD_TO_LIST.items():
        put_all(key, _items(parts.get(heading, [])))

    hero = parts.get("hero products", [])
    put("hero_note", _preamble(hero))
    put_all("hero_items", _items(hero))

    escalation = _items(parts.get("escalation channel", []))
    extra: List[str] = []
    for line in escalation:
        whatsapp = _WHATSAPP.search(line)
        if whatsapp:
            put("whatsapp", whatsapp.group(1))
            continue
        email = _EMAIL.search(line)
        if email:
            put("email", email.group(1))
            continue
        extra.append(line)
    put_all("escalation_extra", extra)

    for suffix, (heading, body) in zip(
        QUESTION_SUFFIXES, _vertical_questions(prompt) + [("", "")] * 3
    ):
        put(f"vertical_heading{suffix}", heading)
        put(f"vertical_body{suffix}", body)

    put("initial_greeting", str(configurations.get("initial_greeting") or ""))
    replies = configurations.get("quick_replies")
    if isinstance(replies, list):
        put_all(
            "quick_replies",
            [
                str(entry.get("label"))
                for entry in replies
                if isinstance(entry, dict) and entry.get("label")
            ],
        )
    tiles = configurations.get("greeting_tiles")
    if isinstance(tiles, list):
        put_all(
            "greeting_tiles",
            [
                " | ".join(
                    [
                        str(tile.get("label") or ""),
                        str(tile.get("prompt") or ""),
                        str(tile.get("image_url") or ""),
                    ]
                )
                for tile in tiles
                if isinstance(tile, dict) and tile.get("label")
            ],
        )

    render_ui = configurations.get("render_ui")
    if isinstance(render_ui, Mapping):
        urls = [str(url) for url in (render_ui.get("trusted_link_urls") or [])]
        # Basket, messaging and anything written into an escalation line are
        # added back automatically on save, so keeping them here would both
        # invite a merchant to delete a link and watch it reappear, and leave
        # a link trusted forever after the line naming it was deleted.
        put_all(
            "trusted_extra",
            [
                url
                for url in urls
                if "wa.me" not in url
                and not url.endswith("/cart")
                and not any(url in line for line in extra)
            ],
        )
    intents = configurations.get("ui_intents")
    if isinstance(intents, Mapping):
        basket = (intents.get("urls") or {}).get("checkout_page")  # type: ignore[union-attr]
        put("cart_url", str(basket or ""))
    return fields


def _vertical_questions(prompt: str) -> List[Tuple[str, str]]:
    """The merchant help section, as its questions and their answers.

    Found by position rather than by name — the heading is a merchant field,
    so a shoe shop's and a rug shop's differ, and matching on the text would
    only ever find the one the blueprint shipped with.

    The section holds up to three questions: the first as the ``###``
    heading that locates the section, the rest as ``####`` sub-headings
    inside it. See ``CommerceVertical.vertical_section`` for why they are
    not three top-level headings.
    """
    end = prompt.find("\n### UI emission")
    if end < 0:
        return []
    start = prompt.rfind("\n### ", 0, end)
    if start < 0:
        return []
    lines = prompt[start + 1 : end].splitlines()
    if not lines:
        return []
    found: List[Tuple[str, str]] = []
    heading, body = lines[0][4:].strip(), []
    for line in lines[1:]:
        if line.startswith("#### "):
            found.append((heading, "\n".join(body).strip()))
            heading, body = line[5:].strip(), []
            continue
        body.append(line)
    found.append((heading, "\n".join(body).strip()))
    return [pair for pair in found if pair[0]][:3]


__all__ = ["fields_from_prompt"]
