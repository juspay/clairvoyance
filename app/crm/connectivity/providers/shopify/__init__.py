"""Shopify — a door with no pipe, and the first connector that ACTS.

One package, its faces:

    actions.py      the fourth verb's face: add_tag, add_note, update_order
    via_nautilus.py the transport those actions use TODAY
    onboard.py      the relay-era handshake, which is no handshake at all

There is no adapter and no template face: canon T11 lists shopify among the
connectors, and ``ConnectorSpec.channel`` and ``.templates`` are both None
for it — a Shopify install is a complete onboarding with nothing to bind, no
address, no send path, and so no message shapes to register.

**Why a transport file exists at all.** Nautilus holds the shops' Shopify
tokens today and performs the writes; clairvoyance will hold them later (the
ruling: "treat nautilus as the shopify connector, and keep the outer wrap the
same even if we move out of nautilus"). So the connector is real here and now
— the plan says ``connector: "shopify", action: "add_tag"`` — while the thing
that carries the write is swappable underneath.

The seam is ``actions._transport()``, and it reads the merchant's own
installation: a shop we onboarded ourselves holds a credential and goes
direct; a shop that has not migrated holds none and travels by relay. Both
can be true on the same day, which is what makes the migration per-shop and
invisible to every published plan.

Moving off nautilus is then four deletions, all BELOW the action's
``perform``: this transport file, its config entry, the relay's own branch,
and the fork in ``_transport``. Nothing in a document, a schema, the
registry, the validator or a stored workflow version changes — which is the
whole point, and the reason the two rules in ``ConnectorAction``'s docstring
(args are the contract, responses are normalised) are not negotiable here.

This file exports nothing on purpose (the re-export-hub scar): import by
full path.
"""
