"""DragonTTS health gate, kill switch, and scheduler monitor.

Submodules:
- :mod:`health` — the ``dragontts:health`` Redis flag state machine (one-shot
  probe, call-time gate, internal writer). Imported by the call-time TTS path,
  so it stays free of ``dispatch`` imports.
- :mod:`kill_switch` — operator-facing kill/restore action + status read
  (the ``/admin/dragontts/{manage,status}`` endpoints).
- :mod:`monitor` — the scheduler task (probe + page). Imported ONLY by
  ``app.main``; deliberately NOT imported here, so importing :mod:`health` from
  the call path never pulls in ``dispatch`` (which would form a cycle).
"""
