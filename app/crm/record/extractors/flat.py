"""The flat shape — handles and facts as top-level payload keys.

Every buddy mirror (lead-api, telephony) sends it, and it is what a new
producer gets by default: the name describes the PAYLOAD, not the
producer. A source that can put ``customer_mobile_number`` at the top
level needs no registration at all.
"""

from typing import Any, Dict

from app.crm.record.schemas import Extracted

# Producer's payload key -> canon attribute name (T05). A producer with a
# different shape brings its own map.
FACT_KEYS = {
    "customer_name": "name",
    "locale": "locale",
    "timezone": "timezone",
}


def extract(payload: Dict[str, Any]) -> Extracted:
    phone = payload.get("customer_mobile_number")
    return Extracted(
        handles={"phone": phone} if phone else {},
        facts={
            attribute: payload[key]
            for key, attribute in FACT_KEYS.items()
            if payload.get(key)
        },
    )
