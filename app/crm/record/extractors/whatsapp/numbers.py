"""number.buddy_moved — our OWN letter: the merchant chose another number
for Buddy to answer on, and Buddy's settings moved with it.

Filed by connectivity (connectivity/letters.py) after the move commits; the
conversations module resolves the old number's open threads when it hears
it. Merchant-level: it names numbers, not a person.

The keys are connectivity's and are spelled again here because rule 12
forbids record importing it; a test pins the two sets equal.
"""

from typing import List

from app.crm.record.extractors.whatsapp.shared import _f
from app.crm.record.schemas import CatalogField


def buddy_moved_fields() -> List[CatalogField]:
    """Which number Buddy left, and which it answers on now."""
    return [
        _f("payload.from_binding_id", "text", "Previous number"),
        _f("payload.to_binding_id", "text", "Buddy's number"),
        _f("payload.channel", "text", "Channel"),
    ]
