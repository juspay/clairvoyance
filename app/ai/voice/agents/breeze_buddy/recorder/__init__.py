"""Meeting recording: the session that owns one recording's lifecycle.

The gate (processors/transcription_gate.py) decides pass-or-capture per
sentence; this package owns everything around it — the meeting row, line
numbering and batched writes, heartbeat, STT rotation, the duration cap and
the idle-timer suspension. Handlers (start/stop) and the template block live
with the other builtins; nothing here imports them.
"""
