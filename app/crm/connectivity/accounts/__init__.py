"""Accounts: a merchant's provider account — connecting it, and the
installation behind each pipe.

Exports nothing (module rules §1: an ``__init__`` is not a re-export hub).
Importers name the file they mean:

    onboarding.py        connect and disconnect an account — generic, for
                         every connector
    installations.py     which installation is behind a pipe, whether it is
                         usable, and its credentials

The numbers under an account, and their settings, live in channels/. The
tables' mechanics stay in ``connectivity/db/`` (the boundary checker
pins a table's SQL to its owning module's db/ package).
"""
