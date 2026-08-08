"""Platform connectors for the commerce flavor.

Each connector registers into the UCP layer's hooks (``ucp/hooks.py``) and
is otherwise invisible: the protocol modules never import a connector, so
adding a platform is adding a package here, never editing UCP code.

Adding connector #2
-------------------

Read ``ucp/hooks.py``'s module docstring first — there is one thing that
must change before a second platform is correct, and it is not obvious
from this package.

Short version: of the three seams, only ``resolve_media`` filters on the
template's ``flavor.<protocol>.connectors``. ``normalize_variants`` and
``repair_description`` do not, so **today Shopify's quirks run for every
commerce tenant**. With one connector that is harmless (the quirks have
narrow triggers and a foreign payload never matches). With two it is a
silent cross-platform bug, so scoping those two chains is part of the
work of adding a connector, not a follow-up to it.

Registering a connector is a side effect of importing its package; the
flavor's ``__init__`` does that import. A new connector therefore needs:

1. its package here, registering into the hooks it actually implements;
2. an import from ``assist/commerce/__init__.py``;
3. its name documented as a legal ``flavor.<protocol>.connectors`` value;
4. the variant/description chains scoped (see above).
"""
