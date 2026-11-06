-- 086: the inbox schema — the conversation tables and the chat_session
-- changes the inbox and Buddy's handoff need, in one file, so the rest of
-- the feature is code only.
--
-- One recorded exception to "one table owner per migration"
-- (docs/crm/migrations.md): the sections below are split by owning module
-- instead.


-- ===========================================================================
-- the conversation tables
-- Owner: conversations.
-- ===========================================================================

-- crm_conversation_session and crm_conversation_message (ADR 0013,
-- ADR 0014; inbox D1 D2 D5 D23 D25): the thread and its timeline. A
-- handoff to the team is four columns on the thread, not a table of its
-- own: ADR 0014 keeps escalation on the thread, and only the open handoff
-- is ever read. Order matters only for the FK — the timeline pins to the
-- thread.


-- ===========================================================================
-- crm_conversation_session — the thread: one per customer per channel, on
-- Buddy's binding (D25), whoever is answering it.
-- ===========================================================================

-- A thread is the unit the Inbox lists and the unit a controller holds.
-- Buddy, a teammate or nobody — which one is answering is DERIVED from
-- this row (an open handoff, an assignee, a bot session), never stored as a
-- word: "who holds it" written down is a second answer that drifts from
-- the first.
--
-- Places this deliberately departs from ADR 0014 (to tell the corpus):
--
-- 1. Keyed by contact_key, not (merchant, customer, channel) (D2). An
--    anonymous widget visitor has no customer yet, so the key is
--    'session:<chat_session id>' until one is known; on WhatsApp it is
--    the customer id. customer_id is stamped separately and may arrive
--    later, so the key never has to change when it does.
-- 2. No 'pending' status (D23), and 'escalated' is four handoff columns
--    rather than a flag: escalated is handoff_requested_at IS NOT NULL, and
--    the others carry what the team needs to act on it.
-- 3. The window is never stored. "Open" is last_inbound_at + the
--    channel's window > now(); the closing message is due at that minus
--    the lead in Buddy's settings. last_inbound_at moves on CUSTOMER
--    messages only.
--
-- 4. No status column: open vs resolved IS resolved_at (NULL = open), so the
--    thread's status is read from it rather than stored beside it — a copy
--    would only ever need a CHECK to keep the two in step.
-- 5. No message.queued letter (the amendment's mirror event): a free-form
--    reply's words live only in crm_conversation_message.body, so they go
--    with the thread at retention (D22), and a workflow's template never
--    appears on a thread. Every outbound row is written by its sender.
--
-- Vocabulary (channel) carries no CHECK — the migration-027 scar.
CREATE TABLE crm_conversation_session (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id       text NOT NULL,
    -- whatsapp · instagram (the channels registry's words) · widget
    -- (conversations' own).
    channel           text NOT NULL,
    -- The customer id, or 'session:<chat_session id>' for a widget
    -- visitor nobody has resolved yet (D2). Lowercase uuid text either
    -- way, so one thread can never be spelled two ways.
    contact_key       text NOT NULL,
    customer_id       uuid,
    -- Where a reply goes: the customer's address as the channel knows it,
    -- and the binding it goes out on (Buddy's binding when the thread
    -- opened, D24 D25). No FK
    -- to crm_channel_binding: connectivity owns that table, and a
    -- schema-level reference would import the boundary into the DDL
    -- (the 050/059 precedent).
    address           text,
    binding_id        uuid,
    -- NULL = open; set = resolved (the thread's status), cleared when her
    -- next message reopens it. Also the retention clock (D22).
    resolved_at       timestamptz,
    -- The teammate holding the thread. NULL = no teammate holds it.
    assignee_user_id  text,
    -- Which Assist agent answers (a template id, snapshotted when Buddy
    -- was handed the thread) and its current chat_session. No FKs: both
    -- live buddy-side, and a session row can be swept independently.
    bot_template_id   uuid,
    bot_session_id    uuid,
    -- The created_at of the last inbound row the bot has answered. "Bot
    -- work pending" is the predicate "an inbound row written after it",
    -- never a stored flag.
    bot_cursor_at     timestamptz,
    -- The window's input (see 3). NULL until the customer writes.
    last_inbound_at   timestamptz,
    -- The open handoff: Buddy asked for a person (handoff_to_human) and the
    -- team has not finished with it. NULL = none, so a thread has at most
    -- one by construction. When it ends, the four are cleared together and
    -- one assignment_trail entry records how (resolved · handed_back ·
    -- sla_lapsed · expired · binding_changed). "Waiting" is a predicate —
    -- requested, no assignee, inside the claim SLA in Buddy's settings —
    -- so a stopped sweep never strands a customer (D5). reason and priority
    -- are vocabulary: code dictionaries, no CHECK (the migration-027 scar).
    handoff_requested_at  timestamptz,
    handoff_reason        text,
    handoff_summary       text,
    handoff_priority      text,
    -- List denormalisations: the Inbox sorts and previews without reading
    -- the timeline. unread is team-level for v1.
    last_message_at   timestamptz NOT NULL DEFAULT now(),
    preview           text,
    unread            boolean NOT NULL DEFAULT false,
    -- Who held it, in order: [{user_id, by, at}], plus one entry per
    -- finished handoff ({action: 'handoff', reason, priority, requested_at,
    -- outcome, by, at}). History, not state — assignee_user_id and the
    -- handoff columns are the present.
    assignment_trail  jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    -- The pin target for the timeline's FK (the 049/058 precedent): every
    -- crm→crm FK is tenant-pinned.
    UNIQUE (merchant_id, id),
    CONSTRAINT crm_conversation_session_contact_key_format
        CHECK (contact_key ~ '^(session:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    CONSTRAINT crm_conversation_session_trail_is_array
        CHECK (jsonb_typeof(assignment_trail) = 'array'),
    -- A handoff is whole or absent: requested_at and priority always come
    -- together, and no reason or summary is left behind without a request.
    CONSTRAINT crm_conversation_session_handoff_shape
        CHECK (
            (handoff_requested_at IS NULL) = (handoff_priority IS NULL)
            AND (
                handoff_requested_at IS NOT NULL
                OR (handoff_reason IS NULL AND handoff_summary IS NULL)
            )
        ),
    -- Composite so a thread can never be attached to another tenant's
    -- customer (the 056 precedent).
    FOREIGN KEY (merchant_id, customer_id)
        REFERENCES crm_customer (merchant_id, id)
);

-- One thread per contact per channel: the projector's upsert target, and
-- the only thing between two inbound letters and two threads.
CREATE UNIQUE INDEX crm_conversation_session_merchant_contact_uq
    ON crm_conversation_session (merchant_id, channel, contact_key);

-- The Inbox list: a merchant's threads, newest first (the views filter on
-- who holds a thread, which is derived per row).
CREATE INDEX crm_conversation_session_merchant_list_ix
    ON crm_conversation_session (merchant_id, last_message_at DESC);

-- "Mine": the threads a teammate holds.
CREATE INDEX crm_conversation_session_merchant_assignee_ix
    ON crm_conversation_session
       (merchant_id, assignee_user_id, last_message_at DESC)
    WHERE assignee_user_id IS NOT NULL;

-- The cross-tenant window sweep (closing message, teammate warnings): it
-- starts from open threads ordered by the customer's last message.
CREATE INDEX crm_conversation_session_window_ix
    ON crm_conversation_session (last_inbound_at)
    WHERE resolved_at IS NULL AND last_inbound_at IS NOT NULL;

-- The retention sweep (D22): resolved threads past 90 days.
CREATE INDEX crm_conversation_session_retention_ix
    ON crm_conversation_session (resolved_at)
    WHERE resolved_at IS NOT NULL;

-- The claim-SLA sweep (D5), across tenants: open handoffs nobody has taken.
CREATE INDEX crm_conversation_session_waiting_ix
    ON crm_conversation_session (handoff_requested_at)
    WHERE handoff_requested_at IS NOT NULL
      AND assignee_user_id IS NULL
      AND resolved_at IS NULL;

CREATE TRIGGER crm_conversation_session_touch
    BEFORE UPDATE ON crm_conversation_session
    FOR EACH ROW EXECUTE FUNCTION crm_touch_updated_at();


-- ===========================================================================
-- crm_conversation_message — the thread's timeline: what the customer said,
-- what we said, and the notes teammates left for each other.
-- ===========================================================================

-- A row POINTS where the truth already lives and caches only what the
-- Inbox needs to render:
--   * inbound  → event_raw_id, the letter in crm_event_raw it came from.
--   * outbound → message_id, the manifest row in crm_message; its ticks
--                (sent · delivered · read) are JOINED from there, never
--                copied, so a late receipt needs no second write here.
--   * note     → teammate-only, never sent anywhere.
-- body is what the Inbox renders (the words, buttons, a media caption);
-- NULL for a template send, which renders from the template registry
-- instead. The manifest keeps no words (canon T16), so a free-form reply's
-- words live only here (departure 5 above).
--
-- No FKs to crm_event_raw or crm_message: record and connectivity own
-- those tables (the 050/059/073 precedent), and crm_event_raw is
-- partitioned with its own retention. The partial uniques on both
-- pointers make a replayed letter or a repeated send write no second row.
--
-- author_kind is vocabulary (customer · assist · teammate) and carries no
-- CHECK; kind is a closed triple the shape CHECKs below depend on.
CREATE TABLE crm_conversation_message (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id              text NOT NULL,
    conversation_session_id  uuid NOT NULL,
    kind                     text NOT NULL,
    author_kind              text NOT NULL,
    -- The teammate, for their replies and notes.
    author_user_id           text,
    event_raw_id             uuid,
    message_id               uuid,
    -- The provider's id for the message (a wamid): what a customer's
    -- quoted reply points at, and what our quoted_message_id names.
    provider_message_id      text,
    body                     jsonb,
    -- When it happened on the channel, not when we wrote the row — the
    -- timeline's order, and the partition key if volume ever calls for it.
    occurred_at              timestamptz NOT NULL DEFAULT now(),
    -- When we wrote the row. An inbound row is stamped under its thread's
    -- row lock, so per thread this rises in commit order: the bot reads
    -- her messages by it (bot_cursor_at), never by occurred_at.
    created_at               timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT crm_conversation_message_kind_check
        CHECK (kind IN ('inbound', 'outbound', 'note')),
    -- Only the customer speaks inbound; the customer never speaks outbound.
    CONSTRAINT crm_conversation_message_inbound_is_customer
        CHECK ((kind = 'inbound') = (author_kind = 'customer')),
    -- A note and a teammate's reply always say which teammate.
    CONSTRAINT crm_conversation_message_teammate_named
        CHECK (
            (kind <> 'note' AND author_kind <> 'teammate')
            OR author_user_id IS NOT NULL
        ),
    CONSTRAINT crm_conversation_message_note_is_teammate
        CHECK (kind <> 'note' OR author_kind = 'teammate'),
    -- Every row can be rendered: it points at its source or carries its
    -- words (a widget message has no letter and no manifest row).
    CONSTRAINT crm_conversation_message_renderable
        CHECK (
            (kind = 'inbound' AND (event_raw_id IS NOT NULL OR body IS NOT NULL))
            OR (kind = 'outbound' AND (message_id IS NOT NULL OR body IS NOT NULL))
            OR (kind = 'note' AND body IS NOT NULL)
        ),
    -- The house pin: a timeline row can never point at another tenant's
    -- thread. The timeline dies with its thread (the retention sweep).
    CONSTRAINT crm_conversation_message_thread_fk
        FOREIGN KEY (merchant_id, conversation_session_id)
        REFERENCES crm_conversation_session (merchant_id, id) ON DELETE CASCADE
);

-- The thread's timeline, newest first — AND the index the cascade delete
-- needs (a FK's columns must lead an index, or every thread the retention
-- sweep removes seq-scans this table).
CREATE INDEX crm_conversation_message_thread_ix
    ON crm_conversation_message
       (merchant_id, conversation_session_id, occurred_at DESC);

-- Replay safety: one timeline row per inbound letter...
CREATE UNIQUE INDEX crm_conversation_message_merchant_event_uq
    ON crm_conversation_message (merchant_id, event_raw_id)
    WHERE event_raw_id IS NOT NULL;

-- ...and one per manifest row we sent.
CREATE UNIQUE INDEX crm_conversation_message_merchant_message_uq
    ON crm_conversation_message (merchant_id, message_id)
    WHERE message_id IS NOT NULL;

-- Which thread shows one of our sends, by the provider's id — the id a
-- delivery receipt names it by.
CREATE INDEX crm_conversation_message_merchant_provider_ix
    ON crm_conversation_message (merchant_id, provider_message_id)
    WHERE provider_message_id IS NOT NULL;


-- ===========================================================================
-- chat_session
-- Owner: buddy.
-- ===========================================================================

-- chat_session.channel and three new ended_reason values (inbox R5 R7).


-- ===========================================================================
-- chat_session
-- ===========================================================================

-- A. channel — which surface the session talks on: web (the widget and
--    the dashboard chat, every row before this) · whatsapp · instagram.
--    Not current_channel (030), which says whether a WIDGET conversation
--    is in chat or voice right now; this one never changes for a session.
--    The idle sweeper reads it to leave inbox (thread-bound) sessions alone
--    — they end with the window (R5), a take-over, or a move of Buddy's
--    binding (R7), not with tab inactivity.
--    Vocabulary: no CHECK (the migration-027 scar — a new channel is a
--    deploy, never a migration).
ALTER TABLE chat_session
    ADD COLUMN IF NOT EXISTS channel varchar(20) NOT NULL DEFAULT 'web';

-- B. ended_reason — the ways a session now ends besides the two it had:
--    window_closed   the channel's reply window ran out (R5)
--    taken_over      a teammate took the thread (Take over)
--    binding_changed Buddy moved to another binding; the session on the
--                    old one ends quietly (R7, D26)
--    The CHECK is widened, not dropped: 027's constraint is the only
--    thing that has ever validated this column, and ChatEndedReason gains
--    the matching members with the code that writes them.
--
--    Dropped and re-added in one statement and validated here: every value
--    027 allowed is still allowed, so existing rows pass. The validation
--    scans chat_session while this file's transaction holds the ACCESS
--    EXCLUSIVE lock, so widget chat waits for that scan during the deploy.
ALTER TABLE chat_session
    DROP CONSTRAINT IF EXISTS chat_session_ended_reason_check,
    ADD CONSTRAINT chat_session_ended_reason_check
    CHECK (
        ended_reason IS NULL
        OR ended_reason IN (
            'user_ended',
            'idle_timeout',
            'window_closed',
            'taken_over',
            'binding_changed'
        )
    );
