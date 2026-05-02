"""Path-prefix -> group-name mapping.

Groups are UI-level clusters the user works with: "configure STT",
"design warm transfer", etc. They are:

* the unit of **question batching** — tightly-coupled fields ask together
* the unit of **approval** — user signs off on one group at a time
* the unit of **progress display** — completed_groups in state

Most specific prefix wins (longest match). Paths with no match fall back
to ``"other"``.
"""

# Ordered most-specific first; the matcher picks the longest prefix.
GROUP_RULES: list[tuple[str, str]] = [
    # --- configurations.* ---
    ("configurations.stt_configuration", "stt"),
    ("configurations.stt_language", "stt"),  # legacy / deprecated
    ("configurations.soniox_context", "stt"),  # legacy
    ("configurations.payload_based_language_selection", "stt"),  # legacy
    ("configurations.tts_configuration_overrides", "tts"),
    ("configurations.tts_configuration", "tts"),
    ("configurations.tts_selection_config", "tts"),
    ("configurations.vad_config", "vad"),
    ("configurations.interruption", "interruption"),
    ("configurations.noise_filter", "audio"),
    ("configurations.keyword_filter", "audio"),
    ("configurations.enable_background_sound", "audio"),
    ("configurations.background_sound_file", "audio"),
    ("configurations.background_sound_volume", "audio"),
    ("configurations.initial_greeting", "greeting"),
    ("configurations.ivr_configuration", "ivr"),
    ("configurations.ivr_greeting", "ivr"),  # legacy
    ("configurations.ivr_goodbye", "ivr"),  # legacy
    ("configurations.ivr_priority", "ivr"),  # legacy
    ("configurations.transfer_number", "warm_transfer"),
    ("configurations.enable_inbound", "inbound"),
    ("configurations.user_idle_configuration", "user_idle"),
    ("configurations.llm_configurations", "llm"),
    # --- top-level template fields ---
    ("expected_payload_schema", "payload_schema"),
    ("expected_callback_response_schema", "callback_schema"),
    ("secrets", "secrets"),
    ("flow", "flow"),
    ("name", "metadata"),
    ("is_active", "metadata"),
    ("outbound_number_id", "metadata"),
    # --- catch-all for configurations.* fields not listed above ---
    ("configurations", "configurations_misc"),
]


def group_for_path(path: str) -> str:
    """Return the group name for ``path`` by longest-prefix match."""
    best: tuple[int, str] = (-1, "other")
    for prefix, name in GROUP_RULES:
        if path == prefix or path.startswith(prefix + "."):
            if len(prefix) > best[0]:
                best = (len(prefix), name)
    return best[1]


# ---------------------------------------------------------------------------
# Which groups the conversational layer can gather by asking the user
# ---------------------------------------------------------------------------
#
# The rest (flow / secrets / schemas / configurations_misc) need a specialist
# because their shape is ``Dict[str, Any]`` or otherwise non-interview-able.

ASKABLE_GROUPS: tuple[str, ...] = (
    "metadata",
    # Call direction up front — drives whether IVR is even relevant.
    "inbound",
    "greeting",
    "stt",
    "tts",
    "vad",
    "interruption",
    "audio",
    "user_idle",
    "llm",
    # Conditional: only relevant for inbound flows.
    "ivr",
    # Conditional: only when warm-transfer is actually needed.
    "warm_transfer",
)


# Groups that should be skipped automatically when their precondition isn't
# met. Keeps the conversational walk short and avoids asking about settings
# the runtime won't even consult.
def should_skip_group(group: str, draft: dict) -> bool:
    """Return True when ``group`` is irrelevant given the current ``draft``.

    Conditional skips:

    * ``ivr`` — only applies when inbound calling is enabled.
    * ``vad`` — Silero VAD is only created when ``turn_detection ==
      smart_turn`` (see ``breeze_buddy/agent/pipeline.py``). For the
      default ``stt_native`` mode (Soniox / Deepgram native endpointing)
      VAD parameters are never read, so we skip the whole group.
    """
    configs = draft.get("configurations") or {}

    if group == "ivr":
        return not configs.get("enable_inbound", False)

    if group == "vad":
        stt = configs.get("stt_configuration") or {}
        # Default in enrichment.yaml is ``stt_native`` (Soniox); only
        # ``smart_turn`` actually consumes the VAD knobs.
        turn_detection = stt.get("turn_detection") or "stt_native"
        return turn_detection != "smart_turn"

    return False


def is_askable(group: str) -> bool:
    return group in ASKABLE_GROUPS


__all__ = [
    "ASKABLE_GROUPS",
    "GROUP_RULES",
    "group_for_path",
    "is_askable",
    "should_skip_group",
]
