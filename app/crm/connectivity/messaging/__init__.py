"""Messaging: everything about a message leaving us — whether it may go,
and how it goes.

Exports nothing (module rules §1: an ``__init__`` is not a re-export hub).
Importers name the file they mean:

    gate.py         may this message reach this person now? Asked before
                    every send; fails closed
    letters.py      message.queued — our own letter on the event spine
                    about a message we are sending
    logs.py         log helpers — sample_ids(), a bounded list of message
                    ids for a log line
    receipts.py     what became of it — the spine consumer that moves a
                    sent message along sent → delivered → read (or failed)
    conversations/  the customer started it — a free-form reply inside the
                    customer-service window, sent now, one reply per send
    outbound/       we start it — an approved template, queued and sent by
                    the dispatcher, one message per send

The send door itself (``connectivity/send.py``) stays at the module root:
CI rule 11 pins it as the one path to the providers. The manifest's
mechanics stay in ``connectivity/db/`` (the boundary checker pins a table's
SQL to its owning module's db/ package).
"""
