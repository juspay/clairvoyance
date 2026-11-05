"""Assist-product flavor packages.

Each subpackage bundles one vertical flavor's entire server surface —
data-bound component schemas, typed UI-intent policy, and any
flavor-specific execution glue — behind lazy-load hooks. The core
engines stay flavor-agnostic:

- ``template/ui_catalog.py`` maps lazy group names to flavor schema
  modules (``LAZY_GROUPS`` + ``ensure_group_loaded``); a flavor's
  schemas import (and self-register) only when a template enables that
  group.
- ``chat/intent_router.py`` maps flavors to intent modules
  (``FLAVOR_INTENT_MODULES`` + ``ensure_flavor_intents``); a flavor's
  intent policy registers only when a session on a flavor-enabled
  template sends a ``ui_intent``.

A process that never sees a flavor-enabled template never imports the
flavor package, and deleting a flavor directory removes the flavor
wholesale (its group simply stops resolving).
"""
