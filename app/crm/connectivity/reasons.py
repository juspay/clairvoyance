"""Every reason a crm_message row can carry — one file, one name each.

`reason` is this module's public vocabulary: merchants read it off the
manifest, channel-lifecycle code watches for specific words, support greps
for them. Definitions scattered across files is how one failure mode drifts
into two spellings, so every constant is declared here and imported by the
file that raises it.

Only the WORDS live here. What each one means for the row — terminal or
retryable, which status it rides with — is decided where it always was:
adapters classify, plan_for_outcome decides.
"""

# --- the send door's refusals (send.py) — all terminal: none of these
# changes by waiting, so a retry would spend the row's attempts
# rediscovering the same missing thing.
REASON_GATE_REFUSED = "gate_refused"
REASON_NO_ADAPTER = "channel_not_supported"
REASON_NO_BINDING = "no_active_binding"
REASON_NO_INSTALLATION = "connector_not_installed"
REASON_INSTALLATION_UNHEALTHY = "connector_unhealthy"
# The T23 registry says this name is not approved for this account — never
# registered, still pending, rejected, deleted, or approved in more than one
# language with nothing on the row to choose between them. ADR 0011: refused
# on OUR side of the wire, so the provider never sees it.
REASON_TEMPLATE_NOT_APPROVED = "template_not_approved"

# Retryable, unlike the refusals above — both mean "no answer", and no
# answer is not "no": the provider may have taken the message. The timeout
# is the deadline firing; send_error is the default case, naming anything
# that escapes classification, one name shared by send()'s catch-all and
# dispatch's outer one.
REASON_TIMEOUT = "send_timeout"
REASON_SEND_ERROR = "send_error"

# --- provider classification (providers/) ---
# A refused connection, a DNS failure and a read timeout all mean the same
# thing everywhere: we don't know whether the provider saw the message.
REASON_TRANSPORT = "transport_error"
# Terminal, and one word for two reporters: require_secret in an adapter
# and send.py's route resolution surface the same broken-credential fact,
# and it is the word channel-lifecycle code watches for on crm_message.
REASON_NO_CREDENTIAL = "connector_credential_missing"
REASON_NO_TEMPLATE = "template_missing"
REASON_BAD_ADDRESS = "recipient_address_invalid"
REASON_BAD_VARIABLES = "template_variables_invalid"
REASON_UNREADABLE = "provider_response_unreadable"

# --- dispatch policy (dispatch.py) ---
# Must match the literal the stale sweep writes in SQL
# (db/queries.py::requeue_stale_claims_query) — dead-by-sweep and
# dead-by-retry are one fact and must carry one word.
REASON_ATTEMPTS_EXHAUSTED = "max_attempts_exhausted"
# Must match its SQL literal in the same sweep: what a reclaimed row carries
# while it waits for its retry. Written only by that UPDATE, but the word is
# merchant-visible on the manifest, so it is declared with the rest of the
# vocabulary rather than living in SQL alone.
REASON_RECLAIMED_STALE_CLAIM = "reclaimed_stale_claim"
# A row with no reason is a support ticket nobody can answer.
REASON_PROVIDER_REJECTED = "provider_rejected"
REASON_SUPPRESSED = "suppressed"
REASON_GATE_UNAVAILABLE = "gate_unavailable"
