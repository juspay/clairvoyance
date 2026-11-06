"""Shared: what both send paths use, however a message goes out — the gate
before it leaves, its receipts after, and the log helpers.

Exports nothing (module rules §1: an ``__init__`` is not a re-export hub).
Importers name the file they mean:

    gate.py         may this message reach this person now? Asked before
                    every send; fails closed
    logs.py         log helpers — sample_ids(), a bounded list of message
                    ids for a log line
    receipts.py     what became of it — the spine consumer that moves a
                    sent message along sent → delivered → read (or failed)
"""
