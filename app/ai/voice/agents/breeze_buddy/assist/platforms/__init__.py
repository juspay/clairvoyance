"""Platforms: one folder per commerce platform — runtime connector AND build-time adapter.

Runtime side (the original "connectors"): a platform package registers into
the UCP layer's hooks (``commerce/ucp/hooks.py``) and is otherwise
invisible. Build-time side: the same package exposes a ``PlatformAdapter``
(``base.py``) that the engine reaches only through ``registry.py``.

Each connector registers into the UCP layer's hooks (``ucp/hooks.py``) and
is otherwise invisible: the protocol modules never import a connector, so
adding a platform is adding a package here, never editing UCP code.

Adding a connector
------------------

1. its package here, registering into the hooks it implements under its
   connector name (hooks run only for templates whose
   ``flavor.<protocol>.connectors`` name it, or name none);
2. an import from ``assist/commerce/__init__.py``;
3. its name added to the documented ``flavor.<protocol>.connectors`` values
   (``FlavorProtocolConfig`` in ``template/types.py``);
4. for onboarding to recognise its stores, a ``PlatformAdapter`` and a
   ``registry.py`` entry. WooCommerce has none yet, so onboarding treats its
   stores as generic.

A platform with no tool endpoint of its own is served by clairvoyance's MCP
endpoint, at ``/mcp/<platform>/<store host>``: add its tool module to
``mcp/in_process.py``'s ``tool_call``. Our engine answers that URL in process;
the public route (``app/api/routers/mcp.py``) is off by default. See
``woocommerce/``.
"""
