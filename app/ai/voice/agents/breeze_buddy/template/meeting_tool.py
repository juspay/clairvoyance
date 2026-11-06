"""Synthesize the meeting-recording start function from template config.

Mirrors ``template/kb_tool.py``: one config block (``meeting_recording``)
drives the tool the LLM sees, so templates do not hand-declare the builtin.
Explicit declarations with the same name still win (``append_kb_tool``), and
per-channel disabling applies like any other function.
"""

from typing import Any, Callable, Dict, Optional

from app.core.logger import logger

_DEFAULT_START_DESCRIPTION = (
    "Start recording the meeting. Call this ONLY when the user clearly asks to "
    "start recording the meeting, for example 'Hey Buddy, start recording the "
    "meeting'. Do not call it for questions about recording. After it returns, "
    "confirm in one short sentence and then stay silent."
)


def synthesize_meeting_recording_function(
    bot_instance: Any,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[Dict[str, Any]]:
    """Return the builtin dict for ``start_meeting_recording`` when the
    template enables meeting recording, else None."""
    configurations = getattr(bot_instance, "configurations", None)
    if configurations is None:
        template = getattr(bot_instance, "template", None)
        configurations = getattr(template, "configurations", None)
    config = getattr(configurations, "meeting_recording", None)
    if config is None or not config.enabled:
        return None

    (log or logger.info)(
        f"Meeting recording: synthesized builtin '{config.start_function_name}'"
    )
    return {
        "type": "builtin",
        "handler": "start_meeting_recording",
        "name": config.start_function_name,
        "description": config.start_description or _DEFAULT_START_DESCRIPTION,
        "properties": {},
        "required": [],
        "cancel_on_interruption": False,
    }
