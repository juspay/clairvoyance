"""IVR (DTMF) subpackage for Breeze Buddy telephony.

- ``selection``: answer-time, pre-pipeline IVR for inbound template selection
  (caller picks which agent/template via keypad before the pipeline is built).
- ``walker``: mid-call IVR mode (``flow.mode == "ivr"``) — a pure DTMF state
  machine that runs the conversation itself, no STT/LLM/pipeline.

Import from the explicit submodules (``ivr.selection`` /
``ivr.walker``); this package intentionally does not re-export.
"""
