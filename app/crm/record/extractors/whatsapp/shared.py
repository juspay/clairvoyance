"""The package's common grammar: the source words, the narrowed-item read
every concern derives through, and the two declaration helpers.

Nothing here is WhatsApp *behaviour* — that lives one file per concern
(inbound · status · template · account · flow) — this is the vocabulary
they share, kept in one place so the concern files stay acyclic: flow may
not import inbound (inbound's ``reply`` reads flow's answer), and both
need the same narrowed-item read.
"""

from typing import Any, Dict, List, Literal

from app.crm.record.schemas import CatalogEntry, CatalogField

SOURCE = "whatsapp"
GROUP = "WhatsApp"


def _item(payload: Dict[str, Any], key: str) -> Dict[str, Any]:
    """The letter's single narrowed item under ``key``, or {}.

    The ingress door files Meta's value with the batched array narrowed to
    ONE item (providers/meta/inbound.py::_narrowed), and the engine's path
    grammar deliberately never indexes arrays — so every deriver about the
    person goes through this read.
    """
    items = payload.get(key)
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return {}


def _f(path: str, type: str, label: str, **flags: Any) -> CatalogField:
    """One declared field; flags mirror CatalogField's keywords."""
    return CatalogField(path=path, type=type, label=label, **flags)  # type: ignore[arg-type]


def _entry(
    topic: str,
    label: str,
    fields: List[CatalogField],
    about: Literal["customer", "merchant"] = "customer",
) -> CatalogEntry:
    """One (source, topic) declaration in the code layer."""
    return CatalogEntry(
        source=SOURCE,
        topic=topic,
        label=label,
        group=GROUP,
        layer="code",
        about=about,
        fields=fields,
    )
