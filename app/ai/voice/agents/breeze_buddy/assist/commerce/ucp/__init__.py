"""UCP — the protocol layer of the commerce flavor.

Everything here speaks Universal Commerce Protocol and nothing else: tool
roles (create_cart / update_cart / get_cart / get_product / search_catalog,
all template-overridable), the wire projections, the intent policies, and
the UI copy. No module in this package names a platform; where a real
gateway needs platform knowledge to interpret, the seam is a hook in
``hooks.py`` and the answer lives under ``connectors/``.
"""
