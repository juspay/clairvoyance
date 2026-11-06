"""The template registry's logic, gathered — the four files that were loose
at the module root and are one concern.

Exports nothing (module rules §1: an ``__init__`` is not a re-export hub).
Importers name the file they mean:

    lifecycle.py    the four transitions — create · submit · edit · retire
    reads.py        every read of the table, including the send-time and
                    publish-time questions
    events.py       the spine consumer: what a provider DECIDED, applied
    retire_guard.py the slot worker_main fills with outreach's count

The table's mechanics stay in ``connectivity/db/`` rather than moving under
here: the boundary checker pins a table's SQL to its owning MODULE's db/
package (rule 1, ``app/crm/<owner>/db/``), and that rule is the corpus's to
change, not this package's.
"""
