"""SQL builders for crm_channel_template (T23, the template registry).

Two rules run through every builder here.

**Tenancy plus account.** Every write carries ``provider_account_ref`` as
well as ``merchant_id``, including the ones that could match on the
provider's globally-unique id alone. It costs nothing and makes it
structurally impossible for one account's webhook to touch another
account's row on a surprising payload.

**Status moves stamp their clock.** ``status_updated_at`` is exposed to
merchants and read by the out-of-order guard, so any statement that changes
``status`` sets it in the same breath.

One deliberate exception, on the webhook path only: a provider letter with
an unusable clock applies its status and LEAVES the stamp where it was.
The column is the ordering key those letters are compared against, and
advancing it to our own clock would make a malformed timestamp outrank
every real provider event that follows. A slightly stale displayed time on
a rare broken letter is worth less than an approval dropped in silence.
"""

from typing import Any, List, Optional, Tuple

from app.core.config.static import (
    CRM_TEMPLATE_CLAIM_CRASHED_AFTER_SECONDS,
    CRM_TEMPLATE_EVENT_SKEW_SECONDS,
)
from app.crm.connectivity.status import (
    TEMPLATE_APPROVED,
    TEMPLATE_DELETED,
    TEMPLATE_DRAFT,
    TEMPLATE_SUBMITTING,
)

TEMPLATE_TABLE = "crm_channel_template"

TEMPLATE_COLUMNS = """
    id, merchant_id, channel, provider_account_ref, name, language,
    provider_template_id, category, submitted_category, category_updated_at,
    components, status, status_updated_at, rejection_reason, quality,
    quality_updated_at, last_synced_at, created_at, updated_at
"""


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def template_by_id_query(merchant_id: str, template_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {TEMPLATE_COLUMNS}
          FROM {TEMPLATE_TABLE}
         WHERE merchant_id = $1
           AND id = $2::uuid
    """
    return query, [merchant_id, template_id]


def template_by_natural_key_query(
    merchant_id: str,
    channel: str,
    provider_account_ref: str,
    name: str,
    language: str,
) -> Tuple[str, List[Any]]:
    """The registry's natural key.

    ``provider_account_ref`` is IN the key, one column beyond canon T23's
    four: a merchant may hold two accounts on one channel (two WABAs), and
    the same name+language registered in both are two different templates
    with two different provider ids. Without it the second one collides with
    the first and the merchant simply cannot register it.
    """
    query = f"""
        SELECT {TEMPLATE_COLUMNS}
          FROM {TEMPLATE_TABLE}
         WHERE merchant_id = $1
           AND channel = $2
           AND provider_account_ref = $3
           AND name = $4
           AND language = $5
    """
    return query, [merchant_id, channel, provider_account_ref, name, language]


def list_templates_query(
    merchant_id: str, channel: Optional[str], status: Optional[str]
) -> Tuple[str, List[Any]]:
    """The console's list. Filters are optional and applied in SQL rather
    than in Python — an IS NULL OR test keeps one statement and one plan
    instead of four hand-built variants."""
    query = f"""
        SELECT {TEMPLATE_COLUMNS}
          FROM {TEMPLATE_TABLE}
         WHERE merchant_id = $1
           AND ($2::text IS NULL OR channel = $2)
           AND ($3::text IS NULL OR status = $3)
         ORDER BY created_at DESC
    """
    return query, [merchant_id, channel, status]


def templates_by_name_query(
    merchant_id: str, channel: str, name: str
) -> Tuple[str, List[Any]]:
    """The publish-time read (rollout phase 08, G12): every row registered
    under this NAME on this channel for this merchant, across provider
    accounts and languages, newest status first. The send door resolves an
    account before it looks a name up; publish cannot know the account yet,
    so it reads them all and templates.template_status judges."""
    query = f"""
        SELECT {TEMPLATE_COLUMNS}
          FROM {TEMPLATE_TABLE}
         WHERE merchant_id = $1
           AND channel = $2
           AND name = $3
         ORDER BY status_updated_at DESC, created_at DESC
    """
    return query, [merchant_id, channel, name]


def approved_template_for_send_query(
    merchant_id: str, channel: str, provider_account_ref: str, name: str
) -> Tuple[str, List[Any]]:
    """The send-time lookup (ADR 0011): is this name APPROVED, and in which
    language?

    Filtered to 'approved' in SQL so the caller's two refusals fall out of
    the row count: zero rows means never registered, still pending, rejected
    or deleted — one fact from the sender's side; more than one means the
    name is approved in several languages and crm_message carries no language
    column to choose with. Both refuse, because guessing which locale a
    customer should receive is not a guess anyone may make.

    LIMIT 2 because the caller only needs to distinguish one from many.

    ``components`` rides along whole — the registered structure, verbatim
    (canon T23 col 11). A send must name where its FLOW buttons sit or Meta
    refuses it (131009), and which button is a Flow button is Meta's
    vocabulary: the provider face reads it off the row
    (providers/whatsapp/payload.py), never this layer.
    """
    query = f"""
        SELECT id, name, language, provider_template_id, category, components
          FROM {TEMPLATE_TABLE}
         WHERE merchant_id = $1
           AND channel = $2
           AND provider_account_ref = $3
           AND name = $4
           AND status = $5
         LIMIT 2
    """
    return query, [merchant_id, channel, provider_account_ref, name, TEMPLATE_APPROVED]


# ---------------------------------------------------------------------------
# The local lifecycle
# ---------------------------------------------------------------------------


def insert_template_draft_query(
    merchant_id: str,
    channel: str,
    provider_account_ref: str,
    name: str,
    language: str,
    components_json: str,
) -> Tuple[str, List[Any]]:
    query = f"""
        INSERT INTO {TEMPLATE_TABLE}
            (merchant_id, channel, provider_account_ref, name, language,
             components)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        RETURNING {TEMPLATE_COLUMNS}
    """
    return query, [
        merchant_id,
        channel,
        provider_account_ref,
        name,
        language,
        components_json,
    ]


def update_draft_components_query(
    merchant_id: str, template_id: str, components_json: str
) -> Tuple[str, List[Any]]:
    """Only ever touches a row that is still 'draft'.

    Re-running "create this draft" must never overwrite a template already
    sent for review: the components on file are the ones the provider is
    looking at, and replacing them locally would make the registry lie about
    what is under review. The status filter is the guard; a caller seeing no
    row raises.
    """
    query = f"""
        UPDATE {TEMPLATE_TABLE}
           SET components = $3::jsonb
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND status = $4
        RETURNING {TEMPLATE_COLUMNS}
    """
    return query, [merchant_id, template_id, components_json, TEMPLATE_DRAFT]


def claim_for_submit_query(merchant_id: str, template_id: str) -> Tuple[str, List[Any]]:
    """draft -> submitting, exclusively.

    The claim is the whole defence against submitting one template twice.
    Two requests that both read 'draft' would both POST to the provider; the
    provider refuses the second by name, but only AFTER we fired it, and the
    local row then records whichever answer landed last.

    'submitting' is deliberately NOT claimable. It looks like it should be —
    "a crashed submit should be retryable" — but a crash after the provider
    accepted leaves a template registered under that name, so the retry
    cannot succeed either; it can only fail differently. Recovery is the
    webhook's resume path below, which matches on the natural key and stamps
    the id the provider already assigned.
    """
    query = f"""
        UPDATE {TEMPLATE_TABLE}
           SET status = $3,
               status_updated_at = now()
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND status = $4
        RETURNING {TEMPLATE_COLUMNS}
    """
    return query, [merchant_id, template_id, TEMPLATE_SUBMITTING, TEMPLATE_DRAFT]


def release_submit_claim_query(
    merchant_id: str, template_id: str
) -> Tuple[str, List[Any]]:
    """submitting -> draft, but only while no provider id was ever stamped.

    Everything between the claim and the provider's acceptance can fail: the
    credential may not resolve, the components may be refused, the process
    may die. Without this the row sits 'submitting' forever — unclaimable by
    the exclusive claim above, and unreachable by the resume path because the
    provider never received it. That template would be dead permanently.

    The ``provider_template_id IS NULL`` guard is what keeps the release
    safe: once an id exists the submission really happened, and only the
    webhook may move the row on.
    """
    query = f"""
        UPDATE {TEMPLATE_TABLE}
           SET status = $3,
               status_updated_at = now()
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND status = $4
           AND provider_template_id IS NULL
        RETURNING {TEMPLATE_COLUMNS}
    """
    return query, [merchant_id, template_id, TEMPLATE_DRAFT, TEMPLATE_SUBMITTING]


def record_submission_query(
    merchant_id: str,
    template_id: str,
    provider_template_id: str,
    category: Optional[str],
    submitted_category: str,
    status: str,
) -> Tuple[str, List[Any]]:
    """The provider accepted it: their id, their category, their status.

    ``submitted_category`` is OURS and is kept beside theirs on purpose — a
    provider that re-categorises MARKETING as UTILITY has changed what the
    merchant pays, and a single column would erase the evidence.
    """
    query = f"""
        UPDATE {TEMPLATE_TABLE}
           SET provider_template_id = $3,
               category = $4,
               submitted_category = $5,
               category_updated_at = now(),
               status = $6,
               status_updated_at = now(),
               rejection_reason = NULL
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND status = $7
        RETURNING {TEMPLATE_COLUMNS}
    """
    return query, [
        merchant_id,
        template_id,
        provider_template_id,
        category,
        submitted_category,
        status,
        TEMPLATE_SUBMITTING,
    ]


def record_in_place_edit_query(
    merchant_id: str,
    template_id: str,
    components_json: str,
    status: str,
    expected_status: str,
) -> Tuple[str, List[Any]]:
    """New components on the SAME row, back to whatever the provider says
    the edit put it in (canon T23's one explicit transition rule: editing an
    approved template re-reviews it in place rather than making a new row).

    ``rejection_reason`` is cleared: the reason described the components that
    were just replaced, and leaving it would attach an old refusal to new
    content.

    ``expected_status`` is the status the caller read before it went to the
    provider, and the write only lands if the row still carries it. Without
    that guard the sequence is: edit() reads 'approved', calls the provider,
    a concurrent retire() sets 'deleted', and this UPDATE puts 'pending' and
    fresh components over the retired row — resurrecting a template the
    merchant withdrew.

    The status the edit is MOVING to is accepted alongside it, because the
    webhook consumer is a second writer on this row and it wins that race
    often: the provider accepts the edit, sends its status letter, and the
    worker applies 'pending' before this statement runs. On a bare equality
    the CAS then matches nothing, edit() raises "reload and try again" —
    advice that cannot work, since 'pending' is not in TEMPLATE_IN_PLACE_EDIT
    — and the components the provider is ALREADY reviewing are never
    recorded. The row then sits at whatever the provider decides with the old
    components under it, forever, because the periodic sync that would have
    healed it was deliberately removed. Accepting the destination status is
    idempotent (this statement sets it anyway) and keeps the tombstone shut:
    'deleted' is neither the expected status nor the destination.
    """
    query = f"""
        UPDATE {TEMPLATE_TABLE}
           SET components = $3::jsonb,
               status = $4,
               status_updated_at = now(),
               rejection_reason = NULL
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND status IN ($4, $5)
        RETURNING {TEMPLATE_COLUMNS}
    """
    return query, [merchant_id, template_id, components_json, status, expected_status]


def lock_template_exclusive_query(key: int) -> Tuple[str, List[Any]]:
    """The retiring side of the template lock (shared/locks.py): held
    EXCLUSIVE for the rest of the transaction — waits for every in-flight
    pinner of this template to commit, and makes later pinners wait for
    the verdict."""
    query = "SELECT pg_advisory_xact_lock($1::bigint)"
    return query, [key]


def retire_template_query(merchant_id: str, template_id: str) -> Tuple[str, List[Any]]:
    """status -> deleted. Never a DELETE: crm_message rows name this template
    by name, and "what did we send in August" must stay answerable."""
    query = f"""
        UPDATE {TEMPLATE_TABLE}
           SET status = $3,
               status_updated_at = now()
         WHERE merchant_id = $1
           AND id = $2::uuid
        RETURNING {TEMPLATE_COLUMNS}
    """
    return query, [merchant_id, template_id, TEMPLATE_DELETED]


# ---------------------------------------------------------------------------
# The webhook path — what a provider DECIDED about a template
# ---------------------------------------------------------------------------
#
# One caller: the spine consumer (connectivity/template_events.py). These
# are the only statements that write provider-decided state, and every one
# of them is a guarded CAS rather than a read followed by a write: two
# letters about one template can be in the pass at the same moment, and the
# WHERE is what decides between them.


def template_by_provider_id_query(
    merchant_id: str, provider_template_id: str
) -> Tuple[str, List[Any]]:
    """The webhook's row, found by the id the PROVIDER assigned.

    ``crm_channel_template_provider_id_uq`` is unique on that column ALONE
    (061 choice 2 — the provider's identifier is globally unique by
    construction), so this matches at most one row anywhere. ``merchant_id``
    still leads the WHERE: the letter's merchant was decided by the ingress
    root's owner lookup, and re-stating it here means a payload naming
    another tenant's template finds nothing rather than something.
    """
    query = f"""
        SELECT {TEMPLATE_COLUMNS}
          FROM {TEMPLATE_TABLE}
         WHERE merchant_id = $1
           AND provider_template_id = $2
    """
    return query, [merchant_id, provider_template_id]


def stale_submit_claims_query(
    merchant_id: str,
    channel: str,
    name: str,
    language: str,
) -> Tuple[str, List[Any]]:
    """Crashed submit claims under one natural key, whatever account holds
    them — the resume's first question, asked before anything else.

    Account-FREE on purpose, and it is the cheap probe rather than the
    answer: a template registered in the provider's own console produces a
    status letter matching no local row on every change, forever, and that
    is an ordinary condition. Asking this first means the ordinary case
    costs one indexed read and says nothing, instead of paying an
    installations lookup and logging a warning about a repair that was never
    needed. The caller resolves the account only once this returns
    something, and picks the row that account holds.

    The situation this repairs: submit() claimed the row, POSTed, and the
    process died before the answer was recorded. The provider DID register
    the template, so the claim can never be released (release requires
    ``provider_template_id IS NULL``, which is true, but the row would then
    be re-submitted under a name the provider already holds) and the
    exclusive claim refuses to re-take it. The template is dead until the
    provider's own status webhook arrives naming an id we have never seen —
    which it does, because Meta sends one for every decision.

    The age predicate is what keeps a HEALTHY submit out of the result —
    see CRM_TEMPLATE_CLAIM_CRASHED_AFTER_SECONDS. Every row carries its own
    provider_account_ref, so the caller can match the one its derived
    account holds; the write's own CAS re-states that account, so a wrong
    pick cannot land.
    """
    query = f"""
        SELECT {TEMPLATE_COLUMNS}
          FROM {TEMPLATE_TABLE}
         WHERE merchant_id = $1
           AND channel = $2
           AND name = $3
           AND language = $4
           AND status = $5
           AND provider_template_id IS NULL
           AND status_updated_at < now() - make_interval(secs => $6)
    """
    return query, [
        merchant_id,
        channel,
        name,
        language,
        TEMPLATE_SUBMITTING,
        CRM_TEMPLATE_CLAIM_CRASHED_AFTER_SECONDS,
    ]


#: A retired template is a TOMBSTONE, and every provider write must respect
#: it. retire_template_query keeps ``provider_template_id`` (crm_message rows
#: name this template and "what did we send in August" must stay answerable),
#: so a webhook naming that id still finds the row long after the merchant
#: withdrew it — and the provider-side delete is best-effort, so Meta may
#: legitimately keep deciding about a template it never removed.
def _not_retired(param: int) -> str:
    return f"AND status <> ${param}"


def _not_older_than(column: str, param: int, skew_param: int) -> str:
    """The out-of-order guard for one stamped column (061 choice 6).

    A provider promises no ordering, so a letter must not overwrite a state
    a LATER letter already wrote. A status ladder would be the wrong test —
    approved -> pending is a legitimate move backwards when a merchant edits
    an approved template — so time is the only honest ordering key, and each
    topic guards on its own column.

    Three things this clause is careful about, each of which silently drops
    a real approval if it is missing:

    * **A letter with no time at all.** ``occurred_at`` is nullable: the
      bay's timestamp read is total, so a provider sending a broken
      ``entry.time`` still files a letter worth applying. ``column <= NULL``
      is NULL, which is not true, which is zero rows — an approval lost to
      a malformed clock. So a NULL parameter skips the guard.
    * **A column with no time yet.** ``category_updated_at`` is NULL until
      a submission records a category, and ``quality_updated_at`` has NO
      writer anywhere before this one — so the FIRST quality letter on
      every row in the table meets a NULL. Without this branch quality
      webhooks would never apply, ever, and nothing would fail loudly.
    * **Second resolution.** Meta stamps ``entry.time`` in whole unix
      seconds while our own transitions stamp ``now()``. A submit at
      10:00:00.7 followed by an approval Meta timestamps 10:00:00 would
      compare as older and be refused — so ours is compared at the
      provider's resolution, not at one they cannot express.

    ``<=`` rather than ``<`` on purpose: the consumer's write commits
    independently of the event row's stamp, so a batch that fails after it
    replays the letter. Re-applying identical values must be a no-op that
    still reports success, not a refusal.

    The clause only works because a letter with no clock does not MOVE the
    clock: every apply writes ``COALESCE($n, <the column>)``, never
    ``now()``. Writing the consumer's own time there would poison this
    predicate with a value no provider event can beat — a malformed
    ``entry.time`` would apply, stamp a moment in OUR present, and then
    refuse the next genuine letter whose provider timestamp is older than
    that. The worker marks such a letter processed, so the registry would
    sit at the state a broken clock left it in, permanently.

    **The skew, and why one is needed at all.** This column is not written
    only by provider letters. Recording a submission and recording an
    in-place edit both stamp it with ``now()`` — OUR clock, at COMMIT — and
    the provider stamps whole seconds at the moment it DECIDED, a round trip
    earlier. So a template the provider approves inside that same second
    yields a letter whose timestamp is legitimately BEHIND our own stamp,
    and a bare comparison refuses it. That is not a rare tie: a 300 ms Graph
    call crosses a second boundary about a third of the time, and the
    WhatsApp edit face reports 'pending' unconditionally, so the refused
    letter was the ONLY source of truth for what the provider decided. The
    row would stay pending forever, the letter marked processed, with no
    periodic sync left to heal it.

    ``CRM_TEMPLATE_EVENT_SKEW_SECONDS`` (10s) is that tolerance: wider than
    any Graph round trip, far narrower than a redelivery, so a letter a few
    seconds late still applies and one minutes late is still refused. The
    principled alternative is a provider-clock column per guarded topic, so
    our own transitions never touch the ordering key at all; that is a
    migration for a window a tolerance closes, and is recorded as the
    follow-up rather than taken here.

    The cost is stated rather than hidden: two PROVIDER letters less than
    the skew apart can now reorder, where before only same-second ones
    could. Both are bounded by the same pre-existing limit — Meta batches
    the changes in one envelope in order, and the pass claims by
    ``received_at``, so a reorder needs two separate deliveries arriving
    backwards inside that window. Last-writer-wins there, knowingly.

    One clause for all three columns rather than a per-column variant: they
    differ only in nullability, and the NULL branch is what a second
    definition would eventually be missing.
    """
    return (
        f"AND (${param}::timestamptz IS NULL\n"
        f"                OR {column} IS NULL\n"
        f"                OR date_trunc('second', {column})\n"
        f"                   <= ${param} + make_interval(secs => ${skew_param}))"
    )


def apply_status_event_query(
    merchant_id: str,
    template_id: str,
    provider_account_ref: str,
    status: str,
    occurred_at: Optional[Any],
    rejection_reason: Optional[str],
) -> Tuple[str, List[Any]]:
    """The provider decided: approved, rejected, paused, deleted, or a word
    we have not seen (061 choice 3 — the vocabulary is theirs and open).

    ``rejection_reason`` is written on every status letter, including as
    NULL: the reason describes the components a provider refused, and
    leaving a stale one attached to a template they have since approved
    tells the merchant to fix something nobody is objecting to.
    """
    query = f"""
        UPDATE {TEMPLATE_TABLE}
           SET status = $4,
               status_updated_at = COALESCE($5::timestamptz, status_updated_at),
               rejection_reason = $6
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND provider_account_ref = $3
           {_not_retired(7)}
           {_not_older_than("status_updated_at", 5, 8)}
        RETURNING {TEMPLATE_COLUMNS}
    """
    return query, [
        merchant_id,
        template_id,
        provider_account_ref,
        status,
        occurred_at,
        rejection_reason,
        TEMPLATE_DELETED,
        CRM_TEMPLATE_EVENT_SKEW_SECONDS,
    ]


def apply_category_event_query(
    merchant_id: str,
    template_id: str,
    provider_account_ref: str,
    category: str,
    occurred_at: Optional[Any],
) -> Tuple[str, List[Any]]:
    """The money one: a provider re-categorised a template on its own, and
    the category is the billing class the merchant pays at.

    ``submitted_category`` is deliberately untouched — it records what WE
    asked for, and 061 choice 4 keeps both columns precisely so the
    difference stays visible instead of one erasing the other.
    """
    query = f"""
        UPDATE {TEMPLATE_TABLE}
           SET category = $4,
               category_updated_at = COALESCE($5::timestamptz, category_updated_at)
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND provider_account_ref = $3
           {_not_retired(6)}
           {_not_older_than("category_updated_at", 5, 7)}
        RETURNING {TEMPLATE_COLUMNS}
    """
    return query, [
        merchant_id,
        template_id,
        provider_account_ref,
        category,
        occurred_at,
        TEMPLATE_DELETED,
        CRM_TEMPLATE_EVENT_SKEW_SECONDS,
    ]


def apply_quality_event_query(
    merchant_id: str,
    template_id: str,
    provider_account_ref: str,
    quality: str,
    occurred_at: Optional[Any],
) -> Tuple[str, List[Any]]:
    """The provider's quality read — the early warning before it pauses a
    template itself. Their word, stored as theirs (GREEN · YELLOW · RED),
    because a merchant comparing our console against the provider's is
    entitled to see the same word twice."""
    query = f"""
        UPDATE {TEMPLATE_TABLE}
           SET quality = $4,
               quality_updated_at = COALESCE($5::timestamptz, quality_updated_at)
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND provider_account_ref = $3
           {_not_retired(6)}
           {_not_older_than("quality_updated_at", 5, 7)}
        RETURNING {TEMPLATE_COLUMNS}
    """
    return query, [
        merchant_id,
        template_id,
        provider_account_ref,
        quality,
        occurred_at,
        TEMPLATE_DELETED,
        CRM_TEMPLATE_EVENT_SKEW_SECONDS,
    ]


def resume_submitted_template_query(
    merchant_id: str,
    template_id: str,
    provider_account_ref: str,
    provider_template_id: str,
    status: str,
    occurred_at: Optional[Any],
    rejection_reason: Optional[str],
) -> Tuple[str, List[Any]]:
    """Stamp the id a crashed submit never got to record, and the status the
    provider is telling us about it, in one statement.

    No time guard here, and that is not an omission: the row has never been
    touched by a provider letter, so there is no later state to regress —
    its ``status_updated_at`` is our own claim's clock. The guard is the
    claim itself. ``status = 'submitting' AND provider_template_id IS NULL``
    is exactly the state this repair is for, so a second letter arriving
    behind the first finds nothing to do, and a submit that completed
    normally in between is not overwritten.

    The clock is still only advanced by a letter that HAS one, for the same
    reason the applies do it: from here on this column is what every later
    provider letter is ordered against, and a resume that stamped our own
    consume time would leave a value no genuine event can beat. Preserving
    the claim's timestamp is strictly safer — it predates every letter that
    can follow.
    """
    query = f"""
        UPDATE {TEMPLATE_TABLE}
           SET provider_template_id = $4,
               status = $5,
               status_updated_at = COALESCE($6::timestamptz, status_updated_at),
               rejection_reason = $7
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND provider_account_ref = $3
           AND status = $8
           AND provider_template_id IS NULL
        RETURNING {TEMPLATE_COLUMNS}
    """
    return query, [
        merchant_id,
        template_id,
        provider_account_ref,
        provider_template_id,
        status,
        occurred_at,
        rejection_reason,
        TEMPLATE_SUBMITTING,
    ]
