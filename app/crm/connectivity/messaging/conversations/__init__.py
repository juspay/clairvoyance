"""Conversations: we reply to the customer inside the channel's
customer-service window — a free-form message, sent now rather than queued.

Exports nothing (module rules §1: an ``__init__`` is not a re-export hub).
Importers name the file they mean:

    abandoned.py    close_abandoned_sessions() — the sweep that closes a
                    reply row a crashed send left behind; the dispatcher
                    runs it on every pass
    session.py      send_session() — one free-form reply: its manifest row,
                    the gate, the send door and its message.queued letter

The gate (``messaging/gate.py``) and the send door (``connectivity/send.py``)
sit outside this folder: every message passes them, not only these.
"""
