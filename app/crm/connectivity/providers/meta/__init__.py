"""Meta's shared vendor layer. WhatsApp, Instagram and Messenger are three
products on ONE Graph API, so the transport lives here and each product's
package holds only what differs. inbound.py is the vendor's inbound face —
one callback, one signature scheme for all three products — answering to
connectivity/ingress.py (boundary rule 11).

Exports nothing: import graph.py by its full path (the re-export-hub scar).
"""
