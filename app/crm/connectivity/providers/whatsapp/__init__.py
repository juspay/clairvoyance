"""WhatsApp, straight at Meta's Cloud API — no aggregator in between.

One package, its faces:

    adapter.py    the send door's child of ChannelAdapter
    classify.py   Meta's error codes, split by "could this succeed later?"
    payload.py    E.164 -> Meta's digits, variables -> template parameters
    onboard.py    Embedded Signup: code -> token -> number -> subscription
    templates.py  the WABA template registry face

The inbound direction is NOT here: Meta's callbacks arrive per APP, not per
product, so that face is vendor-level — providers/meta/inbound.py.

Only the bundle keys live here, because both the send face and the onboarding
face need them and neither may own them: onboarding WRITES the bundle,
adapter and templates READ it, and a drift between those spellings is a
merchant whose sends refuse with "credential missing" against a credential
that exists. That happened once already, between two branches, with
``{"token": ...}`` on one side and ``system_user_token`` on the other.

Boundary rule 11 confines each face to one composition root: adapter.py to
send.py, onboard.py and templates.py to connectors.py. This file is imported
by all of them and is deliberately data-only.
"""

# Canon T11 col 6 names the WhatsApp bundle. Only the first key has a reader
# today; app_secret and verify_token join it when inbound webhooks are
# verified per-merchant rather than per-app.
TOKEN_KEY = "system_user_token"

BUNDLE_KEYS = (TOKEN_KEY,)

#: The channel word the adapter serves. The connector_key that names this
#: package in the CONNECTORS registry is NOT here: that dict IS the
#: vocabulary, so its keys are defined where the dict is, not imported back
#: from the thing they name.
CHANNEL = "whatsapp"
