"""SQL builders for crm_channel_template (T23, the template registry).

Two rules run through every builder here.

**Tenancy plus account.** Every write carries ``provider_account_ref`` as
well as ``merchant_id``, including the ones that could match on the
provider's globally-unique id alone. It costs nothing and makes it
structurally impossible for one account's webhook to touch another
account's row on a surprising payload.

**Status moves stamp their clock.** ``status_updated_at`` is exposed to
merchants and read by the out-of-order guard, so any statement that changes
``status`` sets it in the same breath. A transition with a stale timestamp
is worse than no timestamp: it looks answered.
"""

from typing import Any, List, Optional, Tuple

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
    """
    query = f"""
        SELECT id, name, language, provider_template_id, category
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
    merchant withdrew. Same CAS shape as update_draft_components_query.
    """
    query = f"""
        UPDATE {TEMPLATE_TABLE}
           SET components = $3::jsonb,
               status = $4,
               status_updated_at = now(),
               rejection_reason = NULL
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND status = $5
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


# The webhook path — apply_status_event / apply_category_event /
# apply_quality_event and the crashed-submit resume — is deliberately NOT
# here. Those builders have exactly one caller, the spine consumer that
# turns a provider's template webhook into a status change, and that
# consumer is the next PR. Shipping the SQL ahead of it would leave four
# statements nothing calls, which is the thing a reader cannot tell from a
# statement that is merely unused today.
