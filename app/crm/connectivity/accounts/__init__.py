"""Accounts: a merchant's provider account and the numbers under it.

Exports nothing (module rules §1: an ``__init__`` is not a re-export hub).
Importers name the file they mean:

    onboarding.py        connect and disconnect an account — generic, for
                         every connector
    installations.py     which installation is behind a pipe, whether it is
                         usable, and its credentials
    channel_settings.py  which number templates go out from, which number
                         Buddy answers on, and Buddy's settings

The tables' mechanics stay in ``connectivity/db/`` (the boundary checker
pins a table's SQL to its owning module's db/ package).
"""
