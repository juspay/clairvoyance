"""Xyne, as a code-layer SPEC — a Xyne app's callbacks through the one
decode engine (event-catalog.md §One decode engine).

One concern per file, the registry assembled here (the whatsapp/ shape):

  flow_action.py   flow_action — a person submitted a form an app posted.

A Xyne app event the bay starts filing later (a mention, a direct message)
is one more file beside it and one more entry below.

SOURCE · ENTRIES · DERIVERS are the SPEC_MODULES contract and ALL this file
exports; every other name is imported by full path.
"""

from typing import Dict, List

from app.crm.record.extractors.engine import Deriver
from app.crm.record.extractors.xyne import flow_action
from app.crm.record.schemas import CatalogEntry

SOURCE = "xyne"

DERIVERS: Dict[str, Deriver] = {
    "reply": flow_action.reply,
    "button": flow_action.button,
    "flow_response": flow_action.flow_response,
    "replied_to": flow_action.replied_to,
}

ENTRIES: List[CatalogEntry] = [
    CatalogEntry(
        source=SOURCE,
        topic=flow_action.TOPIC,
        label="Form submitted",
        group="Xyne",
        layer="code",
        about="customer",
        fields=flow_action.fields(),
    ),
]

__all__ = ["SOURCE", "ENTRIES", "DERIVERS"]
