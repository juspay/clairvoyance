"""Shared: what every message passes, however it goes out — the gate before
it leaves, the letter that records it, and its receipts after.

Exports nothing (module rules §1: an ``__init__`` is not a re-export hub).
Importers name the file they mean:

    gate.py         may this message reach this person now? Asked before
                    every send; fails closed
    letters.py      message.queued — our own letter on the event spine
                    about a message we are sending
    receipts.py     what became of it — the spine consumer that moves a
                    sent message along sent → delivered → read (or failed)
"""
