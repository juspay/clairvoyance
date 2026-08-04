"""Commerce flavor (Component Catalog v2, RFC-001).

Lazy-loaded — nothing in this package imports at process start:

- ``schemas`` — the data-bound commerce component schemas
  (ProductCard / ProductGrid / CartView) + their UCP-shape projection
  sub-schemas. Self-registers into the UI catalog via
  ``ui_catalog.register_primitives`` when imported by
  ``ui_catalog.ensure_group_loaded("commerce")`` (triggered by any
  template that enables the ``commerce`` group).
- ``intents`` — the typed UI-intent policy table (add_to_cart /
  remove_line / set_qty / view_product / checkout), the Stage-A cart
  tool-name / state-key constants, and the direct cart executor.
  Self-registers into the intent engine via
  ``intent_router.register_intents`` when imported by
  ``intent_router.ensure_flavor_intents`` (triggered by a ``ui_intent``
  request on a commerce-enabled template).

This ``__init__`` is deliberately import-free so loading the schemas
(any commerce chat session) never drags in the chat intent stack, and
sessions load only the surface they actually use.
"""
