"""SQL builders for crm_connector_installation, crm_channel_binding,
crm_template and crm_message. $1 placeholders only, never interpolation.

Every builder emits a single statement, which Postgres runs atomically — so
nothing here needs a transaction. The message claim and the sweep are
deliberately unscoped by merchant: one global queue, not a loop per tenant.

The vault is deliberately absent: it belongs to app/database, so send.py
reads it through that layer's accessor, never SQL from here.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

INSTALLATION_TABLE = "crm_connector_installation"
BINDING_TABLE = "crm_channel_binding"
TEMPLATE_TABLE = "crm_template"
MESSAGE_TABLE = "crm_message"

INSTALLATION_COLUMNS = """
    id, merchant_id, connector_key, external_account_id, display_label,
    credential_id, status, token_expires_at
"""

BINDING_COLUMNS = """
    id, merchant_id, channel, installation_id, address, capabilities,
    is_primary, status
"""

# Named once so the claim's RETURNING and the decoder cannot drift apart.
# next_attempt_at rides along for the queue-lag log line.
CLAIMED_COLUMNS = """
    id, merchant_id, customer_id, channel, sent_to_address, binding_id,
    source_kind, source_id, purpose_key, template_id, variables,
    dedupe_key, attempt, next_attempt_at
"""


def insert_message_query(
    merchant_id: str,
    customer_id: str,
    channel: str,
    sent_to_address: str,
    source_kind: str,
    source_id: Optional[str],
    purpose_key: str,
    template_id: Optional[str],
    variables: Dict[str, Any],
    dedupe_key: str,
) -> Tuple[str, List[Any]]:
    """One queued row, no verdict (gate-mechanics §1). The dedupe unique
    (merchant_id, dedupe_key) absorbs a producer's retry: conflict = no
    row returned, and the caller treats that as already queued."""
    query = f"""
        INSERT INTO {MESSAGE_TABLE}
            (merchant_id, customer_id, channel, sent_to_address, source_kind,
             source_id, purpose_key, template_id, variables, dedupe_key)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
        ON CONFLICT (merchant_id, dedupe_key) DO NOTHING
        RETURNING id
    """
    return query, [
        merchant_id,
        customer_id,
        channel,
        sent_to_address,
        source_kind,
        source_id,
        purpose_key,
        template_id,
        json.dumps(variables),
        dedupe_key,
    ]


def upsert_installation_query(
    merchant_id: str,
    connector_key: str,
    external_account_id: str,
    display_label: Optional[str],
    credential_id: Optional[str],
    status: str,
    health_detail_json: str,
) -> Tuple[str, List[Any]]:
    """Idempotent on (merchant_id, connector_key, external_account_id) —
    re-onboarding the same WABA updates the existing row in place rather
    than duplicating it (CRM law #4)."""
    query = f"""
        INSERT INTO {INSTALLATION_TABLE}
            (merchant_id, connector_key, external_account_id, display_label,
             credential_id, status, health_detail)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        ON CONFLICT (merchant_id, connector_key, external_account_id)
        DO UPDATE SET
            display_label = COALESCE(EXCLUDED.display_label, {INSTALLATION_TABLE}.display_label),
            credential_id = EXCLUDED.credential_id,
            status = EXCLUDED.status,
            health_detail = EXCLUDED.health_detail
        RETURNING *
    """
    return query, [
        merchant_id,
        connector_key,
        external_account_id,
        display_label,
        credential_id,
        status,
        health_detail_json,
    ]


def upsert_channel_binding_query(
    merchant_id: str,
    channel: str,
    installation_id: str,
    address: str,
    is_primary: bool,
) -> Tuple[str, List[Any]]:
    """Idempotent on (merchant_id, channel, address). is_primary is only
    ever raised here, never lowered by a re-onboard (a later onboard of the
    same number must not silently demote it as someone's default route).
    A re-onboard also reactivates the binding (status = 'active') — the
    caller (onboarding._onboard_in_txn) has already read the row first and
    refused a 'retired' one, since that refusal can't happen inside this
    DO UPDATE."""
    query = f"""
        INSERT INTO {BINDING_TABLE}
            (merchant_id, channel, installation_id, address, is_primary)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (merchant_id, channel, address)
        DO UPDATE SET
            installation_id = EXCLUDED.installation_id,
            is_primary = {BINDING_TABLE}.is_primary OR EXCLUDED.is_primary,
            status = 'active'
        RETURNING *
    """
    return query, [merchant_id, channel, installation_id, address, is_primary]


def get_channel_binding_by_address_query(
    merchant_id: str, channel: str, address: str
) -> Tuple[str, List[Any]]:
    """Read-before-write gate for onboarding's re-connect: the exact
    natural key (merchant_id, channel, address) the upsert above will hit —
    checked first so a 'retired' binding can raise instead of being
    silently resurrected."""
    query = f"""
        SELECT * FROM {BINDING_TABLE}
        WHERE merchant_id = $1 AND channel = $2 AND address = $3
    """
    return query, [merchant_id, channel, address]


def has_primary_binding_query(merchant_id: str, channel: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT 1 FROM {BINDING_TABLE}
        WHERE merchant_id = $1 AND channel = $2 AND is_primary AND status = 'active'
    """
    return query, [merchant_id, channel]


def pause_bindings_for_installation_query(
    merchant_id: str, installation_id: str
) -> Tuple[str, List[Any]]:
    """Part of disconnect's atom — an installation revoked without this
    leaves its bindings claiming to be an active send route. Clears
    is_primary too: otherwise crm_channel_binding_primary_uq (merchant_id,
    channel WHERE is_primary) blocks connecting any new number for this
    channel until someone manually clears the flag on a paused row."""
    query = f"""
        UPDATE {BINDING_TABLE}
        SET status = 'paused', is_primary = false
        WHERE merchant_id = $1 AND installation_id = $2 AND status = 'active'
        RETURNING *
    """
    return query, [merchant_id, installation_id]


def get_installation_query(
    merchant_id: str, installation_id: str
) -> Tuple[str, List[Any]]:
    query = f"SELECT * FROM {INSTALLATION_TABLE} WHERE merchant_id = $1 AND id = $2"
    return query, [merchant_id, installation_id]


def list_installations_query(merchant_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT * FROM {INSTALLATION_TABLE}
        WHERE merchant_id = $1
        ORDER BY installed_at DESC
    """
    return query, [merchant_id]


def disconnect_installation_query(
    merchant_id: str, installation_id: str
) -> Tuple[str, List[Any]]:
    query = f"""
        UPDATE {INSTALLATION_TABLE}
        SET status = 'revoked'
        WHERE merchant_id = $1 AND id = $2
        RETURNING *
    """
    return query, [merchant_id, installation_id]


def get_installation_credential_query(
    merchant_id: str, connector_key: str, external_account_id: str
) -> Tuple[str, List[Any]]:
    """Internal lookup — resolves which credential owns a provider_account_ref
    so templates.py can fetch a decrypted access token. Never exposed
    over the API (credential_id isn't on InstallationRead)."""
    query = f"""
        SELECT id, credential_id, status FROM {INSTALLATION_TABLE}
        WHERE merchant_id = $1 AND connector_key = $2 AND external_account_id = $3
    """
    return query, [merchant_id, connector_key, external_account_id]


def list_active_installations_for_sync_query(
    connector_key: str,
) -> Tuple[str, List[Any]]:
    """Cross-merchant read for the periodic template sync — every healthy
    installation of one connector, across every merchant. Self-scoped via
    crm_connection(), no merchant_id param: the sync job walks all tenants."""
    query = f"""
        SELECT id, merchant_id, external_account_id, credential_id
        FROM {INSTALLATION_TABLE}
        WHERE connector_key = $1 AND status = 'healthy'
    """
    return query, [connector_key]


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
            (merchant_id, channel, provider_account_ref, name, language, components)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        RETURNING *
    """
    return query, [
        merchant_id,
        channel,
        provider_account_ref,
        name,
        language,
        components_json,
    ]


def get_template_by_natural_key_query(
    merchant_id: str, channel: str, provider_account_ref: str, name: str, language: str
) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT * FROM {TEMPLATE_TABLE}
        WHERE merchant_id = $1 AND channel = $2 AND provider_account_ref = $3
          AND name = $4 AND language = $5
    """
    return query, [merchant_id, channel, provider_account_ref, name, language]


def update_draft_components_query(
    merchant_id: str,
    template_id: str,
    provider_account_ref: str,
    components_json: str,
) -> Tuple[str, List[Any]]:
    """Only touches a row that is still status='draft' — resubmitting a
    "create draft" call must never silently overwrite a submitted template."""
    query = f"""
        UPDATE {TEMPLATE_TABLE}
        SET provider_account_ref = $3, components = $4::jsonb
        WHERE merchant_id = $1 AND id = $2 AND status = 'draft'
        RETURNING *
    """
    return query, [merchant_id, template_id, provider_account_ref, components_json]


def update_draft_components_only_query(
    merchant_id: str, template_id: str, components_json: str
) -> Tuple[str, List[Any]]:
    """Component-only edit of a draft (PATCH), unlike create_draft's
    re-upsert — provider_account_ref is left untouched here."""
    query = f"""
        UPDATE {TEMPLATE_TABLE}
        SET components = $3::jsonb
        WHERE merchant_id = $1 AND id = $2 AND status = 'draft'
        RETURNING *
    """
    return query, [merchant_id, template_id, components_json]


def get_template_query(merchant_id: str, template_id: str) -> Tuple[str, List[Any]]:
    query = f"SELECT * FROM {TEMPLATE_TABLE} WHERE merchant_id = $1 AND id = $2"
    return query, [merchant_id, template_id]


def list_templates_query(
    merchant_id: str, channel: Optional[str], status: Optional[str]
) -> Tuple[str, List[Any]]:
    conditions = ["merchant_id = $1"]
    values: List[Any] = [merchant_id]
    if channel is not None:
        values.append(channel)
        conditions.append(f"channel = ${len(values)}")
    if status is not None:
        values.append(status)
        conditions.append(f"status = ${len(values)}")
    query = f"""
        SELECT * FROM {TEMPLATE_TABLE}
        WHERE {' AND '.join(conditions)}
        ORDER BY created_at DESC
    """
    return query, values


def claim_template_for_submit_query(
    merchant_id: str, template_id: str, claimable_statuses: List[str]
) -> Tuple[str, List[Any]]:
    """Compare-and-swap into 'submitting' — the exclusive lock a submit call
    holds before it ever talks to Meta. Exclusive to draft/rejected — a row
    already 'submitting' cannot be reclaimed here; a crashed/retried submit
    resumes via resume_submitted_template_query instead."""
    query = f"""
        UPDATE {TEMPLATE_TABLE}
        SET status = 'submitting', status_updated_at = now()
        WHERE merchant_id = $1 AND id = $2 AND status = ANY($3::text[])
        RETURNING *
    """
    return query, [merchant_id, template_id, claimable_statuses]


def submit_template_query(
    merchant_id: str,
    template_id: str,
    provider_template_id: str,
    category: str,
    submitted_category: str,
    status: str,
) -> Tuple[str, List[Any]]:
    query = f"""
        UPDATE {TEMPLATE_TABLE}
        SET provider_template_id = $3,
            category = $4,
            submitted_category = $5,
            category_updated_at = now(),
            status = $6,
            status_updated_at = now()
        WHERE merchant_id = $1 AND id = $2
        RETURNING *
    """
    return query, [
        merchant_id,
        template_id,
        provider_template_id,
        category,
        submitted_category,
        status,
    ]


def update_approved_components_query(
    merchant_id: str, template_id: str, components_json: str
) -> Tuple[str, List[Any]]:
    """Canon rule: editing an approved template puts the SAME row back to
    pending (Meta re-reviews in place)."""
    query = f"""
        UPDATE {TEMPLATE_TABLE}
        SET components = $3::jsonb, status = 'pending', status_updated_at = now()
        WHERE merchant_id = $1 AND id = $2
        RETURNING *
    """
    return query, [merchant_id, template_id, components_json]


def retire_template_query(merchant_id: str, template_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        UPDATE {TEMPLATE_TABLE}
        SET status = 'deleted', status_updated_at = now()
        WHERE merchant_id = $1 AND id = $2
        RETURNING *
    """
    return query, [merchant_id, template_id]


def sync_template_status_query(
    provider_template_id: str,
    category: Optional[str],
    submitted_category: Optional[str],
    status: Optional[str],
    quality: Optional[str],
    rejection_reason: Optional[str],
) -> Tuple[str, List[Any]]:
    """The drift healer's write — matched by provider_template_id, the one
    key Meta itself guarantees is stable. Only overwrites the columns Meta
    actually reported this round (COALESCE keeps the rest)."""
    query = f"""
        UPDATE {TEMPLATE_TABLE}
        SET category = COALESCE($2, category),
            category_updated_at = CASE
                WHEN $2 IS NOT NULL AND $2 IS DISTINCT FROM category THEN now()
                ELSE category_updated_at
            END,
            submitted_category = COALESCE($3, submitted_category),
            status = COALESCE($4, status),
            status_updated_at = CASE
                WHEN $4 IS NOT NULL AND $4 IS DISTINCT FROM status THEN now()
                ELSE status_updated_at
            END,
            quality = COALESCE($5, quality),
            quality_updated_at = CASE
                WHEN $5 IS NOT NULL AND $5 IS DISTINCT FROM quality THEN now()
                ELSE quality_updated_at
            END,
            rejection_reason = COALESCE($6, rejection_reason),
            last_synced_at = now()
        WHERE provider_template_id = $1
        RETURNING *
    """
    return query, [
        provider_template_id,
        category,
        submitted_category,
        status,
        quality,
        rejection_reason,
    ]


def resume_submitted_template_query(
    merchant_id: str,
    provider_account_ref: str,
    name: str,
    language: str,
    provider_template_id: str,
    category: Optional[str],
    status: Optional[str],
    quality: Optional[str],
    rejection_reason: Optional[str],
) -> Tuple[str, List[Any]]:
    """The resume path for a submit that crashed after Meta accepted the
    template but before the local write of provider_template_id landed —
    matches the natural key of a row still 'submitting' with no
    provider_template_id, instead of the crashed submit re-claiming and
    re-POSTing (Meta would reject the duplicate name). Scoped by
    merchant_id and provider_account_ref (CRM law #1) since the natural
    key alone is not tenant-scoped."""
    query = f"""
        UPDATE {TEMPLATE_TABLE}
        SET provider_template_id = $5,
            category = COALESCE($6, category),
            status = COALESCE($7, status),
            status_updated_at = now(),
            quality = COALESCE($8, quality),
            rejection_reason = COALESCE($9, rejection_reason),
            last_synced_at = now()
        WHERE merchant_id = $1 AND provider_account_ref = $2 AND name = $3
          AND language = $4 AND status = 'submitting'
          AND provider_template_id IS NULL
        RETURNING *
    """
    return query, [
        merchant_id,
        provider_account_ref,
        name,
        language,
        provider_template_id,
        category,
        status,
        quality,
        rejection_reason,
    ]


def claim_queued_messages_query(batch_size: int) -> Tuple[str, List[Any]]:
    """Take up to ``batch_size`` queued rows for this worker.

    SKIP LOCKED steps over rows another worker holds instead of waiting, so
    the loop is safe on every pod at once.

    attempt increments HERE, not after the send, so a worker killed mid-send
    still spends one — otherwise a message that reliably crashes workers is
    retried forever.
    """
    query = f"""
        UPDATE {MESSAGE_TABLE}
           SET status = 'sending',
               claimed_at = now(),
               attempt = attempt + 1
         WHERE id IN (
               SELECT id
                 FROM {MESSAGE_TABLE}
                WHERE status = 'queued'
                  AND next_attempt_at <= now()
                ORDER BY next_attempt_at
                LIMIT $1
                FOR UPDATE SKIP LOCKED
         )
        RETURNING {CLAIMED_COLUMNS}
    """
    return query, [batch_size]


def requeue_stale_claims_query(
    stale_minutes: int, max_attempts: int
) -> Tuple[str, List[Any]]:
    """Requeue rows whose worker never came back — unless they are out of
    attempts, in which case they die here.

    Without the requeue, a pod restart leaves rows in-flight forever:
    invisible to the queue, never sent, and nothing raises.

    Without the attempt check, the sweep loops forever on a row whose outcome
    can never be RECORDED (a duplicate provider_message_id makes apply_outcome
    raise every lap) — claimed, really sent, left 'sending', reclaimed, really
    sent again. The claim spends an attempt per lap, so the ceiling that
    bounds retries bounds this too, and dead-by-sweep gets the same reason as
    dead-by-retry: we stopped, the provider didn't.
    """
    query = f"""
        UPDATE {MESSAGE_TABLE}
           SET status = CASE WHEN attempt >= $2::int
                             THEN 'dead' ELSE 'queued' END,
               reason = CASE WHEN attempt >= $2::int
                             THEN 'max_attempts_exhausted'
                             ELSE 'reclaimed_stale_claim' END,
               claimed_at = NULL
         WHERE status = 'sending'
           AND claimed_at < now() - make_interval(mins => $1::int)
        RETURNING id, status
    """
    return query, [stale_minutes, max_attempts]


def apply_outcome_query(
    message_id: str,
    status: str,
    reason: Optional[str],
    provider_message_id: Optional[str],
    mark_sent: bool,
    attempt: int,
    retry_after_seconds: Optional[int],
) -> Tuple[str, List[Any]]:
    """Record what happened to a claimed message.

    The WHERE clause pins the write to the claim that did the send. Status
    alone is not enough: the sweep can requeue a stale row and a second
    worker reclaim it, putting it back in 'sending' under a NEW claim, and
    the first worker's late outcome would overwrite it. The claim increments
    ``attempt``, making it a claim-generation token — an expired claim's
    write matches zero rows, the same "their outcome wins" answer.

    COALESCE stops a later failure erasing an id an earlier attempt earned.
    ``retry_after_seconds`` is set only when requeuing; NULL leaves
    next_attempt_at alone, since a terminal outcome has no next attempt.
    """
    query = f"""
        UPDATE {MESSAGE_TABLE}
           SET status = $2,
               reason = $3,
               provider_message_id = COALESCE($4, provider_message_id),
               claimed_at = NULL,
               sent_at = CASE WHEN $5 THEN now() ELSE sent_at END,
               next_attempt_at = CASE
                   WHEN $6::int IS NULL THEN next_attempt_at
                   ELSE now() + make_interval(secs => $6::int)
               END
         WHERE id = $1
           AND status = 'sending'
           AND attempt = $7::int
        RETURNING id
    """
    return query, [
        message_id,
        status,
        reason,
        provider_message_id,
        mark_sent,
        retry_after_seconds,
        attempt,
    ]


def primary_binding_query(merchant_id: str, channel: str) -> Tuple[str, List[Any]]:
    """The merchant's default pipe on a channel.

    Only 'active': a paused or retired pipe must produce NO route rather than
    fall through to another number — sending from an unexpected address is
    worse than not sending. is_primary is partial-unique per (merchant,
    channel), so this never has to choose between two rows.
    """
    query = f"""
        SELECT {BINDING_COLUMNS}
          FROM {BINDING_TABLE}
         WHERE merchant_id = $1
           AND channel = $2
           AND is_primary
           AND status = 'active'
    """
    return query, [merchant_id, channel]


def binding_by_id_query(
    merchant_id: str, binding_id: str, channel: str
) -> Tuple[str, List[Any]]:
    """One named pipe, scoped to its merchant AND channel in the WHERE clause
    rather than checked afterwards.

    The channel filter is not redundant with the id: binding_id is a bare
    uuid with no FK, so a row could name a binding of a DIFFERENT channel,
    whose address would then reach this channel's adapter as if it were its
    own kind of endpoint. A mismatch must be 'no route'.
    """
    query = f"""
        SELECT {BINDING_COLUMNS}
          FROM {BINDING_TABLE}
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND channel = $3
           AND status = 'active'
    """
    return query, [merchant_id, binding_id, channel]


def installation_by_id_query(
    merchant_id: str, installation_id: str
) -> Tuple[str, List[Any]]:
    """The account behind a pipe. Status is NOT filtered here — the caller
    decides what an unhealthy installation means, and a route that silently
    disappeared would be reported as 'no connection' when the truth is
    'connection revoked'."""
    query = f"""
        SELECT {INSTALLATION_COLUMNS}
          FROM {INSTALLATION_TABLE}
         WHERE merchant_id = $1
           AND id = $2::uuid
    """
    return query, [merchant_id, installation_id]
