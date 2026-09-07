"""UAP (agentic payments) — Juspay / NammaYatri server-to-server calls.

api.py     transport + credentials; Juspay customers, AOP agents/actions,
           /txns (with the items_canonical validator); NY journeys; the
           onboarding expiry watcher
utils.py   cart canonical builder + intent constraints
ledger.py  the ticket-payment ledger on the chat session

Import by full path (``app.services.uap.api`` / ``.ledger``); this package
re-exports nothing.
"""
