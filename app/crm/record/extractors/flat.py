"""The flat shape — handles and facts as top-level payload keys.

Every buddy mirror (lead-api, telephony) sends it, and it is what a new
producer gets by default: the name describes the PAYLOAD, not the
producer. A source that can put ``customer_mobile_number`` at the top
level needs no registration at all — and the same holds for every row
kind (app/crm/shared/handles.py): a top-level ``xyne_user_id`` is that
handle, spelled the way identity spells it.
"""

from typing import Any, Dict

from app.crm.record.schemas import Extracted
from app.crm.shared.handles import ROW_KINDS

# Producer's payload key -> canon attribute name (T05). A producer with a
# different shape brings its own map.
FACT_KEYS = {
    "customer_name": "name",
    "locale": "locale",
    "timezone": "timezone",
}


def extract(payload: Dict[str, Any]) -> Extracted:
    handles: Dict[str, Any] = {}
    phone = payload.get("customer_mobile_number")
    if phone:
        handles["phone"] = phone
    for kind in ROW_KINDS:
        # Strings only: an id is never a number, a list or an object, and a
        # value that is not one is no handle (resolve() normalizes the rest).
        value = payload.get(kind)
        if isinstance(value, str) and value:
            handles[kind] = value
    return Extracted(
        handles=handles,
        facts={
            attribute: payload[key]
            for key, attribute in FACT_KEYS.items()
            if payload.get(key)
        },
    )
