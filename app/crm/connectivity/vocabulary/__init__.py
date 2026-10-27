"""Vocabulary: the words this module writes and reads, and what each channel
can do — one home for each, so no word is spelled twice.

Exports nothing (module rules §1: an ``__init__`` is not a re-export hub).
Importers name the file they mean:

    status.py       the status words of installations, bindings, messages
                    and templates, and the transition sets that read them
    reasons.py      every reason a message row can carry
    topics.py       the spine topics this module files and reads
    channels.py     what each channel can do — the handle the gate probes,
                    whether it registers templates, its conversation window
"""
