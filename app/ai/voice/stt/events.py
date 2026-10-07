"""Events an STT service fires for the agent, provider-neutral."""

# Fired once, with a reason string, when an STT's connection is gone for good
# (its reconnects are used up). The agent ends the call on it instead of
# letting the bot talk to nobody. A service that fires it sets the class
# attribute ``emits_stt_unavailable = True``.
STT_UNAVAILABLE_EVENT = "on_stt_unavailable"
