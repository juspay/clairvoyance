-- 083: the inbox schema — every table change the inbox and Buddy's handoff
-- need, in one file, so the rest of the feature is code only.
--
-- One recorded exception to "one table owner per migration"
-- (docs/crm/migrations.md): the sections below are split by owning module
-- instead. 084 validates the chat_session CHECK this file re-adds NOT VALID;
-- that must run in its own transaction.


-- ===========================================================================
-- crm_message.pricing_category
-- Owner: connectivity.
-- ===========================================================================

-- crm_message.pricing_category (canon T16; inbox D8): what the provider
-- BILLED a message as.
--
-- Meta prices per message by category (marketing · utility · authentication,
-- and 'service' for a free-form reply inside the customer-service window),
-- and it can bill a template under a different category than the one it was
-- submitted with. The delivery receipt says which one it used, so the
-- receipts consumer records it here: cost reporting reads the provider's
-- verdict, not our guess.
--
-- Nullable and unchecked: NULL until a receipt says, and the vocabulary is
-- the provider's (the 027 scar — a new category is a deploy, never a
-- migration). Not in 056/060's immutability list, so the consumer may write
-- it once a receipt arrives.
ALTER TABLE crm_message
    ADD COLUMN IF NOT EXISTS pricing_category text;


-- ===========================================================================
-- the conversation tables
-- Owner: conversations.
-- ===========================================================================

-- crm_conversation, crm_conversation_message, crm_handoff (ADR 0014; Track
-- B B/01; inbox D1 D2 D3 D5 D23 D25): the thread, its timeline, and every
-- time an agent hands it to the team. Order matters only for the FKs — the
-- timeline and the handoffs pin to the thread.


-- ===========================================================================
-- crm_conversation — the thread: one per customer per channel, on Buddy's
-- binding (D25), whoever is answering it.
-- ===========================================================================

-- A thread is the unit the Inbox lists and the unit a controller holds.
-- Buddy, a teammate or nobody — which one is answering is DERIVED from
-- this row plus crm_handoff (an open team handoff, an assignee, a bot
-- session), never stored as a word: "who holds it" written down is a
-- second answer that drifts from the first.
--
-- Places this deliberately departs from ADR 0014 (to tell the corpus):
--
-- 1. Keyed by contact_key, not (merchant, customer, channel) (D2). An
--    anonymous widget visitor has no customer yet, so the key is
--    'session:<chat_session id>' until one is known; on WhatsApp it is
--    the customer id. customer_id is stamped separately and may arrive
--    later, so the key never has to change when it does.
-- 2. No 'pending' status (D23) and no 'escalated' column: escalated is
--    "an open handoff exists", a predicate over crm_handoff.
-- 3. The window is never stored. "Open" is last_inbound_at + the
--    channel's window > now(); the closing message is due at that minus
--    the lead in Buddy's settings. last_inbound_at moves on CUSTOMER
--    messages only.
--
-- 4. No status column: open vs resolved IS resolved_at (NULL = open), so the
--    thread's status is read from it rather than stored beside it — a copy
--    would only ever need a CHECK to keep the two in step.
--
-- Vocabulary (channel) carries no CHECK — the migration-027 scar.
CREATE TABLE crm_conversation (
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
    -- List denormalisations: the Inbox sorts and previews without reading
    -- the timeline. unread is team-level for v1.
    last_message_at   timestamptz NOT NULL DEFAULT now(),
    preview           text,
    unread            boolean NOT NULL DEFAULT false,
    -- Who held it, in order: [{user_id, by, at}]. History, not state —
    -- assignee_user_id is the present.
    assignment_trail  jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    -- The pin target for the timeline and handoff FKs (the 049/058
    -- precedent): every crm→crm FK is tenant-pinned.
    UNIQUE (merchant_id, id),
    CONSTRAINT crm_conversation_contact_key_format
        CHECK (contact_key ~ '^(session:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    CONSTRAINT crm_conversation_trail_is_array
        CHECK (jsonb_typeof(assignment_trail) = 'array'),
    -- Composite so a thread can never be attached to another tenant's
    -- customer (the 056 precedent).
    FOREIGN KEY (merchant_id, customer_id)
        REFERENCES crm_customer (merchant_id, id)
);

-- One thread per contact per channel: the projector's upsert target, and
-- the only thing between two inbound letters and two threads.
CREATE UNIQUE INDEX crm_conversation_merchant_contact_uq
    ON crm_conversation (merchant_id, channel, contact_key);

-- The Inbox list: a merchant's threads, newest first (the views filter on
-- who holds a thread, which is derived per row).
CREATE INDEX crm_conversation_merchant_list_ix
    ON crm_conversation (merchant_id, last_message_at DESC);

-- "Mine": the threads a teammate holds.
CREATE INDEX crm_conversation_merchant_assignee_ix
    ON crm_conversation (merchant_id, assignee_user_id, last_message_at DESC)
    WHERE assignee_user_id IS NOT NULL;

-- The cross-tenant window sweep (closing message, teammate warnings): it
-- starts from open threads ordered by the customer's last message.
CREATE INDEX crm_conversation_window_ix
    ON crm_conversation (last_inbound_at)
    WHERE resolved_at IS NULL AND last_inbound_at IS NOT NULL;

-- The retention sweep (D22): resolved threads past 90 days.
CREATE INDEX crm_conversation_retention_ix
    ON crm_conversation (resolved_at)
    WHERE resolved_at IS NOT NULL;

CREATE TRIGGER crm_conversation_touch
    BEFORE UPDATE ON crm_conversation
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
-- body is the render cache (the words, buttons, a media caption); NULL for
-- a template send, which renders from the template registry instead. The
-- manifest keeps no words (canon T16), so a free-form reply's words live
-- here and in its message.queued letter (D1).
--
-- No FKs to crm_event_raw or crm_message: record and connectivity own
-- those tables (the 050/059/073 precedent), and crm_event_raw is
-- partitioned with its own retention. The partial uniques on both
-- pointers are what make the projector's replay a no-op.
--
-- author_kind is vocabulary (customer · assist · teammate · workflow — a
-- workflow's template, on a binding it shares with Buddy) and carries no
-- CHECK; kind is a closed triple the shape CHECKs below depend on.
CREATE TABLE crm_conversation_message (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id          text NOT NULL,
    conversation_id      uuid NOT NULL,
    kind                 text NOT NULL,
    author_kind          text NOT NULL,
    -- The teammate, for their replies and notes.
    author_user_id       text,
    event_raw_id         uuid,
    message_id           uuid,
    -- The provider's id for the message (a wamid): what a customer's
    -- quoted reply points at, and what our reply_to quotes.
    provider_message_id  text,
    body                 jsonb,
    -- When it happened on the channel, not when we wrote the row — the
    -- timeline's order, and the partition key if volume ever calls for it.
    occurred_at          timestamptz NOT NULL DEFAULT now(),
    -- When we wrote the row. An inbound row is stamped under its thread's
    -- row lock, so per thread this rises in commit order: the bot reads
    -- her messages by it (bot_cursor_at), never by occurred_at.
    created_at           timestamptz NOT NULL DEFAULT now(),
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
        FOREIGN KEY (merchant_id, conversation_id)
        REFERENCES crm_conversation (merchant_id, id) ON DELETE CASCADE
);

-- The thread's timeline, newest first — AND the index the cascade delete
-- needs (a FK's columns must lead an index, or every thread the retention
-- sweep removes seq-scans this table).
CREATE INDEX crm_conversation_message_thread_ix
    ON crm_conversation_message (merchant_id, conversation_id, occurred_at DESC);

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
-- crm_handoff — one row each time an agent hands a thread to the team
-- (handoff_to_human).
-- ===========================================================================

-- Owned by conversations, not outreach as Track B had it (D3): a handoff
-- is about a THREAD.
--
-- Closing is one statement: UPDATE ... WHERE closed_at IS NULL, so a
-- handoff closes once. Open vs closed IS closed_at — no status column
-- stores it a second time. outcome says how it ended (resolved ·
-- handed_back · sla_lapsed · expired · binding_changed); that list and
-- priority are vocabulary: code dictionaries, no CHECK (the
-- migration-027 scar).
--
-- Not stored: "lapsed" (opened_at + the claim SLA in Buddy's settings <
-- now(), D5), read as a predicate so a stopped sweep never strands a
-- customer.
CREATE TABLE crm_handoff (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id      text NOT NULL,
    conversation_id  uuid NOT NULL,
    -- The chat_session whose agent asked for a person. No FK: the session
    -- lives buddy-side and is swept on its own schedule.
    chat_session_id  uuid NOT NULL,
    reason           text,
    summary          text,
    priority         text NOT NULL DEFAULT 'normal',
    claimed_by       text,
    claimed_at       timestamptz,
    outcome          text,
    -- The teammate who closed it; NULL when the system did.
    closed_by        text,
    closed_at        timestamptz,
    opened_at        timestamptz NOT NULL DEFAULT now(),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    -- A closed handoff always says how it closed; an open one has not.
    CONSTRAINT crm_handoff_closed_shape
        CHECK ((closed_at IS NULL) = (outcome IS NULL)),
    CONSTRAINT crm_handoff_claim_shape
        CHECK ((claimed_by IS NULL) = (claimed_at IS NULL)),
    -- The house pin; handoffs die with their thread (the retention sweep).
    CONSTRAINT crm_handoff_thread_fk
        FOREIGN KEY (merchant_id, conversation_id)
        REFERENCES crm_conversation (merchant_id, id) ON DELETE CASCADE
);

-- A thread's handoffs, newest first — AND the cascade delete's index.
CREATE INDEX crm_handoff_thread_ix
    ON crm_handoff (merchant_id, conversation_id, opened_at DESC);

-- One voice: at most one open handoff per thread.
CREATE UNIQUE INDEX crm_handoff_merchant_open_uq
    ON crm_handoff (merchant_id, conversation_id)
    WHERE closed_at IS NULL;

-- The SLA sweep (D5), across tenants: open handoffs nobody has claimed.
CREATE INDEX crm_handoff_unclaimed_ix
    ON crm_handoff (opened_at)
    WHERE closed_at IS NULL AND claimed_at IS NULL;

CREATE TRIGGER crm_handoff_touch
    BEFORE UPDATE ON crm_handoff
    FOR EACH ROW EXECUTE FUNCTION crm_touch_updated_at();


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
--    Re-added NOT VALID: this file's transaction holds the ACCESS EXCLUSIVE
--    lock the DROP took until it commits, so validating here would scan
--    every chat_session row with widget chat blocked. New and updated rows
--    are checked from now on; 084 validates the existing rows under a lock
--    that lets reads and writes through.
ALTER TABLE chat_session
    DROP CONSTRAINT IF EXISTS chat_session_ended_reason_check;

ALTER TABLE chat_session
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
    ) NOT VALID;


-- ===========================================================================
-- crm_message_receipt_pending
-- Owner: connectivity.
-- ===========================================================================

-- crm_message_receipt_pending: a delivery receipt that arrived before its
-- message's row knew the provider's id.
--
-- The provider's id (a wamid) reaches crm_message when the dispatcher
-- records the send's outcome. A fast webhook can be filed and consumed
-- before that write lands, and then matches no row. Retrying the letter does
-- not help — the event worker offers it again on its very next pass, with no
-- delay, and quarantines it after a handful of tries — so the receipt is
-- parked HERE instead:
--
--   * the dispatcher (and a session send), right after stamping the id,
--     applies what is parked for it — in one atom, a row leaving only with
--     its receipt applied;
--   * the receipts consumer, after parking, tries its row once more, which
--     closes the window where both sides miss each other;
--   * the dispatcher's sweep drains the messages whose row now has the id,
--     then drops anything parked longer than the grace: a receipt that old
--     names a message this system did not send (the provider's own app on
--     the same binding), and it was never an error.
--
-- One row per (message, state): a duplicate receipt parks nothing new. Short
-- lived by design; nothing reads it but the two paths above.
CREATE TABLE crm_message_receipt_pending (
    merchant_id          text NOT NULL,
    provider_message_id  text NOT NULL,
    -- The manifest's word for what happened: sent · delivered · read · failed.
    state                text NOT NULL,
    occurred_at          timestamptz,
    error_code           text,
    pricing_category     text,
    parked_at            timestamptz NOT NULL DEFAULT now(),
    -- merchant_id first (the tenancy law): the provider's id arrives on a
    -- letter, so another tenant's id must never find this row.
    PRIMARY KEY (merchant_id, provider_message_id, state)
);

-- The sweep: everything parked past the grace, oldest first.
CREATE INDEX crm_message_receipt_pending_parked_ix
    ON crm_message_receipt_pending (parked_at);


-- ===========================================================================
-- crm_channel_binding: Buddy's binding
-- Owner: connectivity.
-- ===========================================================================

-- crm_channel_binding: Buddy answers on ONE binding per merchant and channel
-- (inbox R1, D25).
--
-- Buddy's settings (the agent, human handoff, the closing message and its
-- lead, the claim SLA, the non-text reply) live under
-- capabilities["conversation"] on the binding Buddy answers on, and nowhere
-- else: every other binding of the merchant does nothing in the inbox.
-- Choosing another binding MOVES the whole config in one atom
-- (connectivity/settings.py) — it is taken off the old binding and put on
-- the new one, under a lock on the merchant's bindings. This index is the
-- backstop that makes "two bindings both think they are Buddy's" impossible
-- whatever a writer does.
--
-- No status in the predicate: a paused binding keeps the config until the
-- merchant picks another, and the move clears it from wherever it is. Only
-- this change ever writes the key, so no existing row can violate this.
CREATE UNIQUE INDEX crm_channel_binding_buddy_uq
    ON crm_channel_binding (merchant_id, channel)
    WHERE capabilities ? 'conversation';
