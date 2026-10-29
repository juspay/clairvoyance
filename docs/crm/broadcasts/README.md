# Broadcasts

A broadcast is ONE deliberate action: admit a segment into a workflow, now
or on a schedule (canon T17, canon/06-outreach.md). It is the launch button
and nothing else — it never sends (the workflow does) and never owns the
workflow (ten broadcasts may launch the same one). Code:
`app/crm/outreach/broadcasts.py`, `app/crm/outreach/campaigns.py`.

| Phase | Adds |
|---|---|
| 1 | `crm_campaign` (the badge, T22) and `crm_broadcast` (T17); create a campaign; create a DRAFT broadcast, read one, list them |
| 2 | the launch: schedule / cancel, and the freeze — the segment read once, late, into recipient rows (T18) |
| 3 | admitting the frozen recipients into the workflow — its guards as skips with reasons, the rest enrolled with `source_broadcast_id` |
| 4 | the funnel, and closing a broadcast |

## A campaign (the badge)

`POST /campaigns` `{merchant_id, key, name, starts_at?, ends_at?}`

- `key` is the season's stamp (`diwali-2026`): lowercase letters and digits
  joined by single hyphens, at most 64 characters, lowercased on the way in.
  Unique per merchant (409 when taken); it can never be changed.
- `name` is presentation; `starts_at` / `ends_at` are for the report's
  header — a campaign never blocks a send.

## A broadcast (a draft, in phase 1)

`POST /broadcasts` `{merchant_id, name, segment_id, workflow_id, campaign_key?}`

- The segment, the workflow and the campaign must be this merchant's; 422
  names every one that is not, at once. An archived workflow is refused —
  it admits nobody. A draft or paused workflow is allowed: the launch is
  where it must be live.
- Every broadcast is born `draft`, with no clocks set beyond `created_at`.
- `GET /broadcasts?merchant_id=&campaign_key=&limit=&offset=` — newest
  first, with the total; `GET /broadcasts/{id}?merchant_id=` — one (404 for
  an unknown or another merchant's id).

## The launch (phase 2)

`POST /broadcasts/{id}/schedule` `{merchant_id, scheduled_for?}` aims a draft
(or re-aims a scheduled broadcast) at `scheduled_for`, or now when it is left
out. The workflow must be **live**; the moment needs a timezone and may not be
more than a minute past (the grace covers a "now" that arrives after its own
request latency — the freeze then starts at once). Nothing is frozen yet.

At that moment the **broadcaster** — its own worker role,
`CRM_ROLE=broadcaster`, one pod per role like the walker and the dispatcher —
claims the broadcast (`scheduled` → `resolving`, `resolved_at` = now, one
attempt spent) and freezes the segment **once**, page by page, into queued
recipient rows (`crm_broadcast_recipient`, T18); then `resolving` →
`dispatching`. `dispatched_at` stays empty: "fired" is the first admission,
phase 3's. Phase 3 admits the queue into the workflow.

**The role ships dark.** Until a `crm-broadcaster` Deployment exists
(`CRM_ROLE=broadcaster`, 1 replica, no Service — the walker's manifest with
one env value; design/worker-runtime.md's template), nothing claims a
scheduled broadcast: it stays `scheduled`. The new pod adds its own pool
(`POSTGRES_POOL_SIZE` 5 by default, up to 15 with `POSTGRES_MAX_OVERFLOW`) to
the PgBouncer budget every replica is checked against.

- **Late, on purpose.** Friday's send is aimed with Friday's segment: who
  matches at `resolved_at`, not at authoring time. Every page renews
  `resolved_at`, so it ends at the last page: when the audience was frozen.
- **Crash-safe.** `resolved_at` is also the freeze's lease: a freeze whose
  worker died is claimed again 15 minutes after its last page, and resumes
  after the last customer already frozen — the recipients' primary key
  `(broadcast_id, customer_id)` absorbs any overlap.
- **A segment that cannot be answered** (a stored rule record refuses) fails
  the broadcast: `resolving` → `failed`, the reason in `last_error`.
- **Any other error** (a replica timeout, a bug) is kept in `last_error`, and
  the freeze is retried at the next claim. Each claim spends one of
  `attempts`; the error of the last allowed attempt
  (`CRM_BROADCAST_FREEZE_MAX_ATTEMPTS`, 3) fails the broadcast at once, with
  "gave up after 3 attempts: <the error>".
- **A failed broadcast cancels its queued rows** in the same atom: it will
  never dispatch, and a recipient dies at a guard or becomes a run (canon
  T18) — never a `queued` row left behind.
- **A freeze that finishes clears `attempts` and `last_error`**, as a run's
  are cleared by the move that succeeds: a retry that worked leaves nothing
  on the console.
- **Reads the replica, on purpose.** A dynamic segment's pages come from the
  read replica: the freeze only writes ids with `ON CONFLICT DO NOTHING`, and
  an audience a few seconds behind is the design.
- **Ids only.** The freeze reads customer ids only, and each page's insert
  answers with one count. Building an object per person set off the garbage
  collector's full pauses (measured locally on 15 Oct: 8 pauses of ~65 ms per
  2-lakh freeze).

`POST /broadcasts/{id}/cancel?merchant_id=` stops the broadcast admitting
anyone else — its still-queued recipients are cancelled with it, in one
transaction; runs it already started resolve by their own rules (canon). A
freeze running at that moment stops at its next page. 409 once the broadcast
is sent, closed, cancelled or failed.

| From | schedule | the freeze | cancel |
|---|---|---|---|
| `draft` | → `scheduled` | — | → `cancelled` |
| `scheduled` | re-aimed | at its moment → `resolving` | → `cancelled` |
| `resolving` | 409 | → `dispatching` (or `failed`) | → `cancelled`, queue cancelled |
| `dispatching` | 409 | — | → `cancelled`, queue cancelled |
| `sent` · `closed` · `cancelled` · `failed` | 409 | — | 409 |

## Example

[`final-nudge.json`](final-nudge.json): the Diwali season's badge, and a
draft broadcast that will admit "Offered, not granted" into a final-nudge
call workflow. Both bodies are validated in CI against the request models.
