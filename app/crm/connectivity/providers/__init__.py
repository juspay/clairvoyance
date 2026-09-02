"""The adapter registry — the channel vocabulary, in code.

This dict is what the tables deliberately refuse to hold: migration 027
taught that a CHECK constraint turns "support a new channel" into a
migration, a deploy window and a rollback plan. Here a new channel is
providers/<name>/adapter.py plus one line below. Nothing else.

This assembly is the SEND face's door, imported only by send.py. The other
faces of the same packages (onboard.py, templates.py) have their own door in
connectivity/connectors.py — one composition root per face, boundary rule 11:

    grep -rn "connectivity.providers" app/ | grep -v "^app/crm/connectivity/providers/"
"""

from typing import Dict, Optional

from app.crm.connectivity.providers.base import ChannelAdapter
from app.crm.connectivity.providers.whatsapp.adapter import MetaWhatsAppAdapter

# Instantiated once: adapters are stateless request builders.
#
# One entry on purpose — no stand-in adapter. A local run points
# META_WHATSAPP_GRAPH_BASE_URL at a stub and exercises the REAL adapter,
# testing the request we actually send rather than one that resembles it.
ADAPTERS: Dict[str, ChannelAdapter] = {
    MetaWhatsAppAdapter.channel: MetaWhatsAppAdapter(),
    # "sms": SmsAdapter(),
    # "email": EmailAdapter(),
    # "instagram": InstagramAdapter(),
}


def adapter_for(channel: str) -> Optional[ChannelAdapter]:
    """The adapter serving ``channel``, or None.

    None rather than a raise: an unroutable channel is a terminal fact about
    that row, which the caller records as its reason — not an exception the
    worker should wear.
    """
    return ADAPTERS.get(channel)


# MetaWhatsAppAdapter is imported above to BUILD the registry, not to be
# re-exported from it: a package root that hands out its members by name is
# the re-export-hub scar this file's own docstring cites. Anything that
# legitimately needs the class imports it by full path.
__all__ = [
    "ADAPTERS",
    "ChannelAdapter",
    "adapter_for",
]
