# Human Assist Platform Orchestrator

## Goal

Human Assist has one durable lifecycle and many possible delivery platforms.
Native Loom Inbox is the first adapter. Telegram, Slack, Re:amaze, or another
provider can be added without duplicating ticket state, timeout handling,
message storage, ownership, or Buddy-resume behavior.

The integration boundary has exactly three asynchronous operations:

1. `handoff` — open the provider-side conversation with the Buddy/customer
   transcript.
2. `conversation` — carry a customer, merchant, or provider message in either
   direction.
3. `end_conversation` — close from the customer, merchant, provider, timeout,
   session, or system side.

## Runtime shape

```text
Buddy handoff / customer message / merchant action / provider callback
                              |
                              v
                  HumanAssistOrchestrator
        state, ownership, transcript, timeout, close rules
                              |
                              v
                     platform registry
                              |
             +----------------+----------------+
             |                                 |
             v                                 v
       Native Inbox                    Future adapter
    three operations only             three operations only
```

`HumanAssistOrchestrator` is the only lifecycle owner. It resolves the platform
snapshotted on the ticket and invokes the corresponding adapter. An adapter
does provider I/O and normalizes provider messages; it does not write canonical
ticket state or chat rows.

## Responsibilities

| Concern | Owner |
| --- | --- |
| Ticket creation, state transitions, deadlines, and agent ownership | Orchestrator + PostgreSQL |
| Canonical customer, Buddy, and human transcript | Existing `chat_message` storage |
| Per-session mutation serialization | Orchestrator + Redis lock |
| Provider API calls and provider payload normalization | Selected adapter |
| Platform credentials and webhook authentication | Provider integration |
| Human-message exclusion from Buddy evaluation | Shared Human Assist filtering |
| Returning the completed human conversation to Buddy context | Existing transcript |

PostgreSQL remains authoritative. Redis is coordination only and is never the
sole copy of ownership, deadlines, messages, or terminal state.

## Platform selection

The merchant-facing Inbox reads `GET /human-assist/platforms`. The response is
generated from the adapter registry, so Inbox does not contain a hardcoded
platform list.

A platform button updates `widget_config.human_assist_platform`. The choice is:

- scoped to the selected merchant's active widget configurations;
- applied only to new handoffs; and
- snapshotted in
  `chat_session.metadata.human_assist.metadata.platform`.

An active conversation therefore continues on its original platform even when
the merchant changes the destination for later tickets.

## Adding a platform

Create one adapter module, set its registry metadata, and implement the three
operations:

```python
class ExampleHumanAssistPlatform(HumanAssistPlatform):
    spec = HumanAssistPlatformSpec(
        key="example",
        display_name="Example",
        description="Route new handoffs to Example.",
    )

    async def handoff(self, context, event):
        # Create the provider conversation and pass event.transcript if needed.
        return PlatformOperationResult(conversation_ref="provider-ticket-id")

    async def conversation(self, context, event):
        # Deliver outbound events or normalize event.raw_payload inbound.
        return PlatformOperationResult(messages=(...))

    async def end_conversation(self, context, event):
        # Resolve provider state for any event.initiator.
        return PlatformOperationResult(provider_state={"resolved": True})
```

Register one instance in `app/services/human_assist/platforms/__init__.py`.
Its key then becomes available through the platform API and automatically
appears as an Inbox button. Platform keys are validated by shape instead of a
database enum, so registering an adapter requires no schema migration.

Provider callback handlers should authenticate the provider request, resolve
the internal conversation, and pass the raw event to
`relay_platform_human_assist_event` or `end_platform_human_assist`. The selected
adapter's `conversation` or `end_conversation` operation performs the
provider-specific interpretation.

## Lifecycle details

### Handoff

The orchestrator filters out human-agent rows, creates the authoritative
pending ticket, and calls the selected adapter with the Buddy/customer
history. Provider references and state are merged into the ticket's existing
metadata JSONB. A provider failure closes the ticket as `platform_error` and
returns control to Buddy.

The first Human Assist request uses the customer's current `chat_session`. If
that session has already had a handoff, the next request atomically:

1. asks Buddy's configured LLM for a compact continuity summary;
2. creates a new `chat_session` with the same template, tenant, widget
   metadata, and copied `agent_session_state`;
3. stores the continuity summary as an internal-context Buddy message;
4. ends the previous session with `human_assist_rollover`; and
5. makes the new session its own pending Human Assist ticket.

The `chat_session` UUID is also the Human Assist ticket ID. This context is not
a handoff reason or a second ticket record. Buddy replay and the merchant
Inbox read it from the canonical `chat_message` transcript, and future
adapters receive it in the handoff event's transcript. It is hidden from the
customer transcript and excluded from Buddy evaluation. A dedicated
`session_rollover` SSE event
carries the replacement `session_id` and a new session-bound widget token so
the embed can switch to the new chat without using credentials from the ended
session. `human_assist_status` remains a public lifecycle-only event.
The limited Buddy history query pins this internal continuity row, so a long
human exchange cannot push it out of the replay window before Buddy resumes.

### Conversation

All directions use the same operation:

- `customer` sends storefront text toward the provider;
- `merchant` sends a claimed Inbox response toward the customer/provider; and
- `platform` normalizes an authenticated provider event into canonical
  customer or human messages.

The orchestrator persists the normalized output tagged with `sender_type`
(a typed `chat_message` column: customer, buddy, human, system, or
internal). Human rows remain visible in the transcript but are excluded
when Buddy performance is evaluated. The platform itself is not re-tagged
per message — it is already snapshotted once on the ticket
(`chat_session.metadata.human_assist.metadata.platform`).

### End conversation

The same operation receives an explicit initiator:

- `customer` for storefront disconnect;
- `merchant` for the Inbox close button;
- `platform` for provider-side close;
- `timeout` when nobody claims the ticket;
- `session` when the underlying session ends; or
- `system` for orchestration/provider failure.

The orchestrator attempts provider cleanup and then closes the authoritative
ticket, restoring Buddy or ending the session according to the close reason.
A provider cleanup failure is recorded in the ticket metadata but does not
trap the merchant in an uncloseable ticket. Because the human exchange is in
the existing chat transcript, Buddy has that context after merchant close
without a transcript copy.

For native storefront presence, the open SSE stream refreshes
`customer_last_seen_at`. If the stream disappears, the lifecycle sweep closes
the ticket after `HUMAN_ASSIST_CUSTOMER_DISCONNECT_TIMEOUT_SECONDS`. This grace
period avoids falsely closing a ticket during a page refresh or same-site
navigation; no browser polling or immediate `pagehide` close is used.
`HUMAN_ASSIST_LIFECYCLE_LOOP_INTERVAL_SECONDS` controls sweep resolution, and
the shared scheduler wakes at the tighter of that value and its general
background-task cadence. See [Configuration](#configuration) below for where
each of these values lives and how live-tunable it actually is.

## Configuration

| Name | Location | Default | Live-tunable? |
| --- | --- | --- | --- |
| `HUMAN_ASSIST_CLAIM_TIMEOUT_SECONDS` | `app/core/config/dynamic.py` | 300s (5 min) | Yes — read once per ticket, at creation/rollover time |
| `HUMAN_ASSIST_CUSTOMER_DISCONNECT_TIMEOUT_SECONDS` | `app/core/config/dynamic.py` | 45s | Yes — read on every lifecycle sweep tick |
| `HUMAN_ASSIST_PLATFORM_OPERATION_TIMEOUT_SECONDS` | `app/core/config/dynamic.py` | 30s | Yes — read on every adapter call |
| `HUMAN_ASSIST_LIFECYCLE_LOOP_INTERVAL_SECONDS` | `app/core/config/dynamic.py` | 5s | No in practice — `BackgroundTaskScheduler.register_task` binds `interval_seconds` once at startup and never re-reads it, so a change only takes effect after the next pod restart |

All four are DevCycle/Redis-backed `dynamic.py` accessors (`get_config(...)`),
which lets them share the same override mechanism as every other runtime
knob even though the last one is not actually live-tunable. Its own docstring
carries that caveat so the inconsistency is documented at the source, not
just here.

## All db schema's to be created or used

| Classification | Schema object | Purpose |
| --- | --- | --- |
| Created by migration 044 | `idx_chat_session_human_assist_inbox` | Scoped Inbox status/activity reads |
| Created by migration 044 | `idx_chat_session_human_assist_claim_deadline` | Ordered pending-ticket timeout sweeps |
| Created by migration 044 | `idx_chat_session_human_assist_customer_seen` | Ordered disconnected-customer sweeps |
| Altered by migration 044 | `chat_message.sender_type` varchar | Buddy/customer/human/system/internal attribution; `NULL` for ordinary chat |
| Created by migration 044 | `chat_message_sender_type_check` constraint | Validates `sender_type` against the five known values |
| Altered by migration 044 | `chat_session.handoff_happened` boolean | Durable record that a handoff occurred |
| Altered by migration 044 | `chat_session.metadata.human_assist` JSONB record constraints | Durable status, ownership, deadlines, platform snapshot, provider state, and close details |
| Created by migration 044 | `chat_session_human_assist_record_check` and `chat_session_handoff_record_check` constraints | Validate lifecycle shape and require a durable record when handoff history is set |
| Altered by migration 044 | `chat_session.current_channel` constraint | Adds the `HUMAN` routing state |
| Altered by migration 044 | `widget_config.human_assist_enabled` boolean | Merchant enable/disable setting |
| Altered by migration 044 | `widget_config.human_assist_platform` varchar | Open-ended adapter key for new handoffs |
| Altered by migration 044 | `chat_session.ended_reason` constraint | Adds `human_assist_rollover` for repeat-handoff session replacement |
| Reused | `chat_session` and `chat_message` | Canonical session and complete transcript |
| Reused | `agent_session_state` | Copies generic cart, checkout, and client-context state into the successor session |
| Not created | Separate Human Assist ticket table | Avoided; one `chat_session` is one ticket and uses the same UUID |
| Not created | Per-platform ticket/message tables | Avoided; adapters share the authoritative lifecycle and transcript |
| Not created | Platform enum or platform registry table | Avoided; the code registry allows new adapters without schema changes |
| Not created | `platform_message_id` dedup key | No shipped adapter supplies an external id yet (native never does); add via a migration when a real webhook-based adapter needs it |

### Migration locking

Migration 044's five `CHECK` constraints use plain `ADD CONSTRAINT`, matching
every other constraint migration in this codebase — none of them use the
`NOT VALID` / `VALIDATE CONSTRAINT` split. That split only lightens the lock
(`SHARE UPDATE EXCLUSIVE` instead of `ACCESS EXCLUSIVE` for the pre-existing-row
scan) when the `ADD CONSTRAINT ... NOT VALID` and its later
`VALIDATE CONSTRAINT` run in separate transactions. The migration runner
(`scripts/migrate.py`) wraps an entire migration file in one
`conn.transaction()` regardless of the file's own `BEGIN`/`COMMIT`, so the
split would provide no real benefit here — `chat_session`, `chat_message`,
and `widget_config` are under `ACCESS EXCLUSIVE` for this migration's entire
runtime either way. `CREATE INDEX CONCURRENTLY` is not used for the same
reason: Postgres refuses to run it inside any transaction block, and this
runner never runs migration statements outside one.

## Defensive decoding

`decode_human_assist_conversation` treats a persisted row it cannot safely
represent as absent rather than raising: a null `widget_config_id`, an
unrecognized `status` or `close_reason`, or a missing `requested_at` /
`claim_deadline_at` / `customer_last_seen_at` / `last_activity_at` all
short-circuit to `None`.
Every caller (`list_human_assist_conversations`,
`get_human_assist_conversation`, `list_due_human_assist_claims`,
`list_stale_human_assist_customers`, ...) already filters `None` decode
results, so one malformed row is silently skipped instead of failing the
whole Inbox list, transcript fetch, or sweep. The `chat_session_human_assist_record_check`
constraint above is the primary integrity guarantee; this is defense in
depth for rows written before the constraint existed or by a path that
bypassed it.
