"""Meeting recording for a desk assistant — in memory, event-driven.

Nothing here is wired into the processor chain and nothing is stored on our
side. The feature attaches through extension points the pipeline already
exposes: a user-mute strategy on the aggregator (speech never reaches the
LLM while recording), an observer on the task (collects lines and hears the
stop phrase), RTVI server messages to the client (``recording_started``,
``recording_ended``, ``recording_lines``) and the template's HTTP hooks for
delivery. State lives in ``MeetingSession`` for the duration of the call.
"""
