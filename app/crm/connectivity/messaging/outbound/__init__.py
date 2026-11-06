"""Outbound: a message we start — an approved template, proposed now and
sent by the dispatcher, one message per send.

Exports nothing (module rules §1: an ``__init__`` is not a re-export hub).
Importers name the file they mean:

    queue.py        queue_message() — how anything outside connectivity
                    proposes a send; writes the manifest row, sends nothing
    dispatch.py     the dispatcher — claims queued rows, runs the gate,
                    hands each to send(), and walks the retry ladder

The gate (``messaging/shared/gate.py``) and the send door
(``connectivity/send.py``) sit outside this folder: every message passes
them, not only these. The manifest's mechanics stay in ``connectivity/db/``
(the boundary checker pins a table's SQL to its owning module's db/
package).
"""
