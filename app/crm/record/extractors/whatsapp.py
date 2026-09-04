"""Meta's WhatsApp letters — which of them is about a PERSON, and which
about the merchant's own things.

The bay (connectivity's Meta face) files four kinds of letter under this
source, and the pass needs one answer from each of them before any consumer
runs: whose letter is this? Without an entry here the flat extractor
answers for them, finds no ``customer_mobile_number``, and every letter is
quarantined ``no_handle`` — template approvals included, which makes the
webhook consumer downstream a function nothing ever calls.

The discriminator is the provider's own shape, not our topic word: the
registry hands an extractor the PAYLOAD and nothing else, so this file
reads what Meta actually put in the ``value`` object.

  * ``statuses`` / ``messages`` present — a delivery receipt or a reply.
    Those are about a person, and finding her is C6's half of the work
    (the receipt walker and the inbound arm). Until it lands they keep
    today's behaviour exactly: no handle, so the pass quarantines them
    loudly and replayably. Claiming they are merchant-level instead would
    stamp them processed with a NULL customer — forever, wrongly — and
    silently destroy the attribution C6 is written to make.
  * anything else — a template review, a category or quality change, an
    account notice. Meta names no person in these BY DESIGN (canon T13 col
    14), so they are merchant-level: the pass skips resolve(), stamps a
    NULL customer, and still hands the letter to every consumer.
"""

from typing import Any, Dict

from app.crm.record.schemas import ABOUT_MERCHANT, Extracted

#: The two keys that make a Meta ``value`` object about a person. Both are
#: arrays in Meta's own shape; presence is the whole test, because the bay
#: narrows them to one item and never removes them.
_CUSTOMER_KEYS = ("messages", "statuses")


def extract(payload: Dict[str, Any]) -> Extracted:
    if any(key in payload for key in _CUSTOMER_KEYS):
        # C6's half. Deliberately the flat extractor's empty answer rather
        # than a guess: the phone is in messages[].from / statuses[].
        # recipient_id, and the arm that reads it also owns what to do with
        # the customer it resolves.
        return Extracted()
    return Extracted(about=ABOUT_MERCHANT)
