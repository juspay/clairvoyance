"""Channels: what each channel can do, and the merchant's numbers on it.

Exports nothing (module rules §1: an ``__init__`` is not a re-export hub).
Importers name the file they mean:

    registry.py     what each channel can do — the handle the gate probes,
                    whether it registers templates
    settings.py     which number templates go out from, which number Buddy
                    answers on, and Buddy's settings

The bindings' mechanics stay in ``connectivity/db/`` (the boundary checker
pins a table's SQL to its owning module's db/ package).
"""
