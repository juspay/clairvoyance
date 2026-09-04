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


# --- what a provider's error code MEANS, for the row -------------------------
#
# Adapters classify on the provider's code and hand it up untouched — that is
# their contract, and the code is what an engineer matches against the
# provider's documentation, so it stays in the log line. But the row is read
# by people who do not have that documentation open: a merchant on the
# manifest, support grepping, an operator at 2am. "190" is a lookup task;
# "token_expired" is the answer.
#
# So the code is translated once, at the moment it is written
# (dispatch._dispatch_one), and only for codes we have named. Everything else
# — every word above, and any code not in this table — passes through
# unchanged, so nothing is ever lost or invented.
PROVIDER_CODE_REASONS = {
    # The connection is broken: every queued message for this merchant is
    # about to fail the same way.
    "10": "permission_denied",
    "190": "token_expired",
    "200": "permission_denied",
    "131005": "access_denied",
    "133010": "number_not_registered",
    # This message was wrong; the connection is fine.
    "100": "invalid_parameter",
    "131008": "required_parameter_missing",
    "131009": "parameter_value_invalid",
    "131026": "recipient_cannot_receive_whatsapp",
    "131030": "recipient_not_in_allowed_list",
    "131047": "outside_24h_window",
    "132000": "template_variable_count_mismatch",
    "132001": "template_not_found",
    "132005": "template_too_long",
    "132007": "template_policy_violation",
    "132012": "template_variable_format_invalid",
    "132015": "template_paused",
    "132016": "template_disabled",
    # Pacing, not a verdict: these ride status=failed with a retry.
    "4": "provider_rate_limited",
    "613": "provider_rate_limited",
    "80007": "account_rate_limited",
    "131056": "pair_rate_limited",
    "130429": "throughput_limit_reached",
    "131048": "spam_rate_limit_reached",
    "131049": "engagement_limit_reached",
    "429": "provider_rate_limited",
}


def readable_reason(reason):
    """The word a row should carry for this reason.

    Total and lossless: a provider code we have named becomes its meaning, a
    code we have not stays exactly as the adapter reported it (so a new code
    is still greppable and still matches the provider's docs), and one of
    this file's own words is already the answer and passes through.
    """
    if reason is None:
        return None
    return PROVIDER_CODE_REASONS.get(reason, reason)
