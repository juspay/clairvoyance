"""XyneSpaces' relay bay. Webhooks arrive via go.webhooks.cc as generic
`flow_action` events (the transport the merchant's flow emits), so there is
no per-product layer — one callback, one signature scheme, one source word.
inbound.py is the vendor's inbound face, answering to connectivity/ingress.py
(boundary rule 11).

Exports nothing: import inbound.py by its full path (the re-export-hub scar).
"""
