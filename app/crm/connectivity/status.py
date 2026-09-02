"""The three status vocabularies this module writes, and the transition sets
that read them.

ONE file, three sections, rather than three files: the vocabularies are small
(7 · 5 · 3 words) and two of them are read TOGETHER at the only place that
moves both — onboarding, where a door's status and its pipes' statuses change
in one atom. Three modules would make that file import three names to state
one transition, and would suggest the three lists change independently. They
do not.

**These words are OPEN sets, not closed ones.** Migration 061 ships no CHECK
on ``crm_channel_template.status`` and says why: the status vocabulary is the
PROVIDER's, "Meta has renamed categories before and will add statuses we have
not seen", and the provider face normalises an unknown word to lowercase
rather than rejecting it. So this file is not a validator and must never grow
into one. It is the one home for the words we BRANCH on, so that a filter and
the statement it filters cannot drift apart — the scar accounts.py opens with,
where "usable installation" had two definitions and the day they diverged the
miss was silent.

The transition sets follow the same rule: they gate an action WE are about to
take ("may I edit this template in place?"), never the acceptance of a word
arriving FROM a provider. A status we have never seen is simply absent from
every set, which already means "we do not act on it". If a webhook path ever
grows ``if status not in <set>: raise``, that is the gate 061 forbids.
"""

# ---------------------------------------------------------------------------
# crm_channel_template (T23) — the registry. Provider-owned, open.
# ---------------------------------------------------------------------------

#: Ours, not the provider's: the row exists locally and has never been shown
#: to anyone. Migration 061 makes it the column default.
TEMPLATE_DRAFT = "draft"
#: Ours: the exclusive claim a submit holds while the provider call is in
#: flight, so two callers cannot both POST the same name.
TEMPLATE_SUBMITTING = "submitting"
TEMPLATE_PENDING = "pending"
TEMPLATE_APPROVED = "approved"
TEMPLATE_REJECTED = "rejected"
TEMPLATE_PAUSED = "paused"
#: Retiring a template writes 'deleted', never 'retired' — 'retired' belongs
#: to a BINDING. Never an SQL DELETE: crm_message rows name the template, and
#: "what did we send in August" must stay answerable.
TEMPLATE_DELETED = "deleted"

#: The only status a local draft edit applies to. Everything else has been
#: shown to a provider and has to go back through one.
TEMPLATE_LOCAL_EDIT = frozenset({TEMPLATE_DRAFT})

#: Statuses a provider that edits in place will re-review from. 'rejected' is
#: in this set and NOT in the submittable set, which is the whole fix for a
#: dead end that existed both ways: re-submitting a rejected template POSTs a
#: create for a name the provider already holds ("name already exists"), and
#: refusing to edit it left no way to correct the components either.
TEMPLATE_IN_PLACE_EDIT = frozenset(
    {TEMPLATE_APPROVED, TEMPLATE_REJECTED, TEMPLATE_PAUSED}
)

# ---------------------------------------------------------------------------
# crm_connector_installation (T11) — the door.
# ---------------------------------------------------------------------------

#: Migration 060's column default: a row that exists before anything about it
#: is proven.
INSTALLATION_CONNECTING = "connecting"
INSTALLATION_HEALTHY = "healthy"
INSTALLATION_DEGRADED = "degraded"
#: The merchant disconnected. A status change, never a DELETE.
INSTALLATION_REVOKED = "revoked"
#: An OPS decision ("this merchant is switched off"). The upsert's WHERE
#: refuses to touch such a row, so a merchant re-running signup cannot undo it.
INSTALLATION_DISABLED = "disabled"

#: The only installation state anything may act through — fail closed on
#: everything else, 'connecting' included. Onboarding verifies the token and
#: the endpoint against the provider and writes the row 'healthy' directly, so
#: 'connecting' is an unproven connection with no first-use deadlock to earn it
#: an exception.
INSTALLATION_USABLE = frozenset({INSTALLATION_HEALTHY})

# ---------------------------------------------------------------------------
# crm_channel_binding (T12) — the pipe.
# ---------------------------------------------------------------------------

#: Migration 060's column default.
BINDING_ACTIVE = "active"
#: A disconnect pauses the pipes under the door; a re-onboard brings them back.
BINDING_PAUSED = "paused"
#: Canon T12 col 10: a retired pipe SURRENDERED its address and the provider
#: may have recycled it to someone else, so re-onboarding must RAISE rather
#: than resurrect it.
BINDING_RETIRED = "retired"
