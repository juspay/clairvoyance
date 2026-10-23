"""binding.buddy_moved — our OWN letter: the merchant chose another binding
for Buddy to answer on, and Buddy's settings moved with it.

Filed by connectivity (connectivity/letters.py) after the move commits; the
conversations module resolves the old binding's open threads when it hears
it. Merchant-level: it names bindings, not a person.

The keys are connectivity's and are spelled again here because rule 12
forbids record importing it; a test pins each key declared here to one
letters.py writes.
"""

from typing import List

from app.crm.record.extractors.whatsapp.shared import _f
from app.crm.record.schemas import CatalogField


def buddy_moved_fields() -> List[CatalogField]:
    """Which binding Buddy left, and which it answers on now."""
    return [
        _f("payload.from_binding_id", "text", "Previous binding"),
        _f("payload.to_binding_id", "text", "Buddy's binding"),
        _f("payload.channel", "text", "Channel"),
    ]
