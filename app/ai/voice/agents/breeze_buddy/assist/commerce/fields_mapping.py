"""The store's form, mapped to and from the template it builds.

``brand_block_from_fields`` writes the answers into the prompt's brand block,
``update_chat_settings`` into the template's settings, and
``read_brand_facts`` and ``read_chat_settings`` read them back out, so the
facts are kept only in the template itself. ``update_brand_facts`` saves
edited answers into an existing prompt, keeping every line the form does not
know where it was.
"""

from __future__ import annotations

import re
from typing import Any, Collection, Dict, List, Mapping, Optional, Sequence, Tuple

from app.ai.voice.agents.breeze_buddy.assist.commerce.skeleton import COMMERCE_V2

_URL = re.compile(r"https://[^\s<>\"'`|]+")

# Where brand_block_from_fields writes each field, for read_brand_facts to
# read it back from.
_BRAND_HEAD = "## Brand identity"
_IDENTITY_LABELS = {
    "Assistant name": "assistant_name",
    "Brand": "brand_line",
    "Tagline / positioning": "tagline",
}
_ONE_LINE_SECTIONS = {
    "What we sell": "what_we_sell",
    "Vocabulary register": "vocabulary",
    "Compliance note": "compliance",
}
_LIST_SECTIONS = {
    "Hero products": "hero_items",
    "Trust signals": "trust_items",
    "Active offers": "offer_items",
    "Returns and exchanges": "returns",
    "Delivery": "delivery",
    "Common questions": "faq",
    "Escalation channel": "help_links",
}
# The order a new brand block is written in.
_SECTION_ORDER = (
    "What we sell",
    "Hero products",
    "Trust signals",
    "Vocabulary register",
    "Active offers",
    "Returns and exchanges",
    "Delivery",
    "Common questions",
    "Compliance note",
    "Escalation channel",
)
_ESCALATION_KEYS = ("whatsapp", "email", "help_links")
_HEADING = re.compile(r"#{1,6} ")
_LABEL_LINE = re.compile(r"- \*\*(.+?):\*\* (.+)")
_WHATSAPP_LINE = re.compile(r"- Primary: \*\*WhatsApp / phone (.+)\*\*")
_EMAIL_LINE = re.compile(r"- Email: `(.+)`")


def _first(fields: Mapping[str, Sequence[str]], key: str) -> str:
    values = fields.get(key) or []
    return values[0] if values else ""


def _bullets(values: Sequence[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        if value:
            out.append(value if value.startswith("- ") else f"- {value}")
    return out


def _section(title: str, lines: Sequence[str]) -> List[str]:
    """A ``###`` block, or nothing when it has nothing to say."""
    body = [line for line in lines if line]
    return [f"### {title}", "", *body, ""] if body else []


def _tile_from_text(line: str) -> Optional[Dict[str, str]]:
    """``label | prompt | image url`` → a greeting tile; None without an image."""
    parts = [part.strip() for part in str(line).split("|")]
    if len(parts) < 3 or not parts[0] or not parts[2].startswith("https://"):
        return None
    return {"label": parts[0], "prompt": parts[1] or parts[0], "image_url": parts[2]}


def _urls(lines: Sequence[str]) -> List[str]:
    """Every https address written into free-text lines."""
    return [hit.rstrip(".,;:)]}") for line in lines for hit in _URL.findall(str(line))]


def _whatsapp_url(number: str) -> str:
    """A wa.me link, only for a number written with its country code ("+91
    98765 43210"): "080-31708114" or "98765 43210" would open the wrong chat."""
    if not number.strip().startswith("+"):
        return ""
    digits = "".join(character for character in number if character.isdigit())
    return f"https://wa.me/{digits}" if digits else ""


def _write_identity_line(label: str, fields: Mapping[str, Sequence[str]]) -> str:
    value = _first(fields, _IDENTITY_LABELS[label])
    return f"- **{label}:** {value}" if value else ""


def _write_section_lines(title: str, fields: Mapping[str, Sequence[str]]) -> List[str]:
    if title in _ONE_LINE_SECTIONS:
        return [_first(fields, _ONE_LINE_SECTIONS[title])]
    if title == "Escalation channel":
        whatsapp = _first(fields, "whatsapp")
        email = _first(fields, "email")
        return [
            f"- Primary: **WhatsApp / phone {whatsapp}**" if whatsapp else "",
            f"- Email: `{email}`" if email else "",
            *_bullets(fields.get("help_links") or []),
        ]
    return _bullets(fields.get(_LIST_SECTIONS[title]) or [])


def _section_fields(title: str) -> Tuple[str, ...]:
    if title == "Escalation channel":
        return _ESCALATION_KEYS
    return (_ONE_LINE_SECTIONS.get(title) or _LIST_SECTIONS[title],)


def brand_block_from_fields(fields: Mapping[str, Sequence[str]]) -> str:
    """The brand block written from the store's fields, under the headings
    the template studio uses (taken from #1209). A line or section with
    nothing in it is left out rather than shown empty: nothing is invented,
    the merchant fills it on the Build page."""
    identity = [
        _write_identity_line("Assistant name", fields),
        _write_identity_line("Brand", fields),
        "- **Storefront:** `{shop_url}`",
        _write_identity_line("Tagline / positioning", fields),
    ]
    sections = [
        line
        for title in _SECTION_ORDER
        for line in _section(title, _write_section_lines(title, fields))
    ]
    return "\n".join(
        [_BRAND_HEAD, "", *(line for line in identity if line), "", *sections]
    ).rstrip()


def _quick_reply_text(reply: Mapping[str, Any]) -> str:
    return str(reply.get("label") or reply.get("value") or "").strip()


def _tile_to_text(tile: Mapping[str, Any]) -> str:
    return f"{tile.get('label')} | {tile.get('prompt') or ''} | {tile.get('image_url')}"


def update_chat_settings(
    fields: Mapping[str, Sequence[str]],
    configurations: Optional[Mapping[str, Any]] = None,
) -> Dict[str, object]:
    """Configuration entries the fields imply, already config-shaped.

    Only fields that are present are written, so a blueprint default
    stays until the merchant sets its field. A quick reply or tile the
    merchant did not change keeps everything the form does not show (a
    reply's message, action and icon; a tile as it was written), taken from
    ``configurations``. Links the assistant may show as buttons (WhatsApp,
    and any address in the help links) are added to the trusted list.
    """
    old = configurations or {}
    values: Dict[str, object] = {}
    if "initial_greeting" in fields:
        values["initial_greeting"] = _first(fields, "initial_greeting")
    if "quick_replies" in fields:
        replies = {
            _quick_reply_text(reply): reply
            for reply in old.get("quick_replies") or []
            if isinstance(reply, Mapping)
        }
        values["quick_replies"] = [
            dict(replies.get(label) or {})
            or {"label": label, "value": label, "action": None, "icon": None}
            for label in fields.get("quick_replies") or []
            if label
        ]
    if "greeting_tiles" in fields:
        tiles_now = {
            _tile_to_text(tile): tile
            for tile in old.get("greeting_tiles") or []
            if isinstance(tile, Mapping)
        }
        tiles = (
            dict(tiles_now[line]) if line in tiles_now else _tile_from_text(line)
            for line in fields.get("greeting_tiles") or []
        )
        values["greeting_tiles"] = [tile for tile in tiles if tile]
    trusted = [
        url
        for url in [
            _whatsapp_url(_first(fields, "whatsapp")),
            *_urls(fields.get("help_links") or []),
        ]
        if url
    ]
    if trusted:
        values["render_ui"] = {"trusted_link_urls": list(dict.fromkeys(trusted))}
    return values


def read_chat_settings(configurations: Mapping[str, Any]) -> Dict[str, List[str]]:
    """The greeting, quick replies and tiles the settings hold, written as the
    form writes them: the reverse of ``update_chat_settings``, so the merchant
    edits what shoppers see now (the blueprint's, until it is changed)."""
    out: Dict[str, List[str]] = {}
    greeting = configurations.get("initial_greeting")
    if isinstance(greeting, str) and greeting.strip():
        out["initial_greeting"] = [greeting.strip()]
    replies = [
        _quick_reply_text(reply)
        for reply in configurations.get("quick_replies") or []
        if isinstance(reply, Mapping)
    ]
    if any(replies):
        out["quick_replies"] = [reply for reply in replies if reply]
    tiles = [
        _tile_to_text(tile)
        for tile in configurations.get("greeting_tiles") or []
        if isinstance(tile, Mapping) and tile.get("label") and tile.get("image_url")
    ]
    if tiles:
        out["greeting_tiles"] = tiles
    return out


def _find_brand_block(prompt: str) -> Optional[Tuple[int, int]]:
    """Where the brand block sits: from its heading up to the operating
    block (or the end), or None when the prompt has none."""
    if prompt.startswith(_BRAND_HEAD):
        start = 0
    else:
        start = prompt.find(f"\n{_BRAND_HEAD}") + 1
        if start == 0:
            return None
    end = prompt.find(f"\n{COMMERCE_V2.operating_head}", start)
    return start, end if end >= 0 else len(prompt)


def _split_brand_block(block: str) -> Tuple[List[str], List[Tuple[str, List[str]]]]:
    """The brand block's identity lines, then each heading with its lines,
    as written (line endings only trimmed)."""
    identity: List[str] = []
    sections: List[Tuple[str, List[str]]] = []
    for line in block.splitlines()[1:]:
        line = line.rstrip()
        if _HEADING.match(line):
            sections.append((line, []))
        elif sections:
            sections[-1][1].append(line)
        else:
            identity.append(line)
    return identity, sections


def _join_wrapped_lines(lines: Sequence[str]) -> List[List[str]]:
    """The lines, each with the indented lines that continue it (a tagline
    written over two lines, a bullet with its sub-points, is one entry)."""
    entries: List[List[str]] = []
    for line in lines:
        if entries and line[:1].isspace() and line.strip():
            entries[-1].append(line)
        else:
            entries.append([line])
    return entries


def _read_identity_line(entry: Sequence[str]) -> Optional[Tuple[str, str]]:
    """(label, value) for an identity entry the form knows, else None."""
    label = _LABEL_LINE.fullmatch(entry[0])
    if not label or label.group(1) not in _IDENTITY_LABELS:
        return None
    value = " ".join([label.group(2), *(line.strip() for line in entry[1:])])
    return label.group(1), value


def _form_section_title(heading: str) -> Optional[str]:
    title = heading[4:].strip() if heading.startswith("### ") else ""
    return title if title in _SECTION_ORDER else None


def _notes(lines: Sequence[str]) -> List[str]:
    """A list section's lines that are not ``- `` items (a rule for the
    model, a plain sentence): not a field, so never shown or dropped."""
    return [
        line
        for entry in _join_wrapped_lines(lines)
        if entry[0].strip() and not entry[0].startswith("- ")
        for line in entry
    ]


def _read_section_values(title: str, lines: Sequence[str]) -> Dict[str, List[str]]:
    """A known section's fields: a list's ``- `` items one value each (its
    sub-points joined in); its other lines are notes, see ``_notes``."""
    if title in _ONE_LINE_SECTIONS:
        # Its paragraphs as written, blank lines between them included.
        text = "\n".join(lines).strip("\n")
        return {_ONE_LINE_SECTIONS[title]: [text]} if text else {}
    out: Dict[str, List[str]] = {}
    for entry in _join_wrapped_lines(lines):
        if not entry[0].startswith("- "):
            continue
        line = " ".join(part.strip() for part in entry)
        whatsapp = _WHATSAPP_LINE.fullmatch(line)
        email = _EMAIL_LINE.fullmatch(line)
        if title == "Escalation channel" and whatsapp and "whatsapp" not in out:
            out["whatsapp"] = [whatsapp.group(1)]
        elif title == "Escalation channel" and email and "email" not in out:
            out["email"] = [email.group(1)]
        else:
            out.setdefault(_LIST_SECTIONS[title], []).append(line.removeprefix("- "))
    return out


def read_brand_facts(prompt: str) -> Optional[Dict[str, List[str]]]:
    """The fields read back out of a built template's prompt: the reverse of
    ``brand_block_from_fields``; None when the prompt has no brand block.

    A line or section the form does not know (the storefront line, a
    hand-added section, a note in a list) is not a field:
    ``update_brand_facts`` keeps it as it is.
    """
    span = _find_brand_block(prompt)
    if span is None:
        return None
    identity, sections = _split_brand_block(prompt[span[0] : span[1]])
    fields: Dict[str, List[str]] = {}
    for entry in _join_wrapped_lines(identity):
        known = _read_identity_line(entry)
        if known:
            fields.setdefault(_IDENTITY_LABELS[known[0]], [known[1]])
    seen = set()
    for heading, lines in sections:
        title = _form_section_title(heading)
        if title is None or title in seen:
            continue
        seen.add(title)
        for key, values in _read_section_values(title, lines).items():
            fields.setdefault(key, values)
    return fields


def update_brand_facts(
    prompt: str, fields: Mapping[str, Sequence[str]], edited: Collection[str] = ()
) -> str:
    """``prompt`` with the brand lines of the ``edited`` fields written from
    ``fields``, in place; a field newly filled is added after the rest.

    Everything else is kept as it is, where it is: an identity line or
    section not edited, the storefront line, a hand-added section, a note in
    a list, and everything outside the brand block byte for byte.
    ``ValueError`` when the prompt has no brand block.
    """
    span = _find_brand_block(prompt)
    if span is None:
        raise ValueError("prompt has no brand block")
    start, end = span
    block = prompt[start:end]
    identity, sections = _split_brand_block(block)

    kept: List[str] = []
    written = set()
    for entry in _join_wrapped_lines(identity):
        known = _read_identity_line(entry)
        if known and known[0] not in written and _IDENTITY_LABELS[known[0]] in edited:
            # The edited name replaces the whole entry; a cleared one drops it.
            if _write_identity_line(known[0], fields):
                kept.append(_write_identity_line(known[0], fields))
        else:
            kept.extend(entry)
        if known:
            written.add(known[0])
    kept.extend(
        _write_identity_line(label, fields)
        for label in _IDENTITY_LABELS
        if label not in written and _write_identity_line(label, fields)
    )
    identity_text = "\n".join(kept).strip("\n")
    out: List[str] = [
        _BRAND_HEAD,
        "",
        *(identity_text.split("\n") if identity_text else []),
        "",
    ]

    seen = set()
    for heading, lines in sections:
        title = _form_section_title(heading)
        if (
            title is not None
            and title not in seen
            and any(key in edited for key in _section_fields(title))
        ):
            body = _write_section_lines(title, fields)
            notes = [] if title in _ONE_LINE_SECTIONS else _notes(lines)
            out.extend(_section(title, [*(n for n in notes if n not in body), *body]))
        else:
            out.extend([heading, *lines])
        if title is not None:
            seen.add(title)
    for title in _SECTION_ORDER:
        if title not in seen:
            out.extend(_section(title, _write_section_lines(title, fields)))

    rewritten = "\n".join(out).rstrip()
    gap = block[len(block.rstrip()) :]
    return prompt[:start] + rewritten + gap + prompt[end:]


__all__ = [
    "brand_block_from_fields",
    "read_brand_facts",
    "read_chat_settings",
    "update_brand_facts",
    "update_chat_settings",
]
