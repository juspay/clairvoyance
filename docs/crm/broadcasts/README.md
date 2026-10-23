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
| 4 | pause and resume, the report (the funnel), and closing a broadcast |

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

| From | schedule | the broadcaster | cancel | pause / resume (phase 4) |
|---|---|---|---|---|
| `draft` | → `scheduled` | — | → `cancelled` | 409 |
| `scheduled` | re-aimed | at its moment → `resolving` | → `cancelled` | 409 |
| `resolving` | 409 | the freeze → `dispatching` (or `failed`) | → `cancelled`, queue cancelled | 409 |
| `dispatching` | 409 | admission → `sent` | → `cancelled`, queue cancelled | pause → `paused` |
| `paused` | 409 | — (the queue waits) | → `cancelled`, queue cancelled | resume → `dispatching` |
| `sent` | 409 | the reporting window ends → `closed` | 409 | 409 |
| `closed` · `cancelled` · `failed` | 409 | — | 409 | 409 |

## Admission (phase 3)

The same broadcaster loop admits what the freeze queued
(`outreach/admission.py`; canon/06-outreach.md: "apply the target workflow's
guards → enrol everyone else into `workflow_id`, stamping
`source_broadcast_id` on each run"). Every recipient ends one of two ways:

- **`enrolled`** — a run of the workflow started through its first door,
  stamped `source_broadcast_id` (canon T20 col 18), its context her current
  phone and her name (a broadcast has no founding event);
- **`skipped`** with its reason (canon T18 col 9):

| Reason | When |
|---|---|
| `already_in_flow` | re-entry is off and she ran this plan before, or a run of hers is still open (on any of her records) |
| `cooldown_active` | her last run began inside the plan's cooldown |
| `no_phone` | the plan calls or messages, and she has no phone |
| `customer_gone` | erased, or not this merchant's |
| `key_required` | the plan was republished "one run per <field>" after it was scheduled |
| `workflow_not_live` | the plan was archived |
| `admission_failed` | an unexpected error on her row — logged, skipped once, never retried forever |

How a page goes, at the broadcaster's own pace (no throttle — the walker
and the dialler own their limits):
- **Claim** up to 1,000 queued rows → `claimed` (who, since when), `FOR
  UPDATE SKIP LOCKED`: two pods never take one person.
- **Gather once**: the workflow, the page's customers in one identity read
  (`get_customers`), the merchant's merges (`merged_pairs`).
- **One transaction per person**: the guards, her run and her row's end share
  one fate — never a run without `enrolled`, never `enrolled` without a run.
  A customer merged after the freeze is admitted as her surviving record.
- **Spread**: each run's first wake is spread over 60 seconds — mass writes
  must never come due the same second (canon T20 col 8).
- The first page stamps `dispatched_at` ("fired"); when nothing waits, the
  broadcast is **`sent`**.

What stops it:
- **cancel** — the person being admitted finishes, everyone after is
  `cancelled` (the broadcast row is held `FOR SHARE` by each admission,
  `FOR UPDATE` by the cancel);
- **a paused workflow holds the queue** (rows go back to `queued`, nothing is
  skipped), the walker's own rule for a paused plan; it resumes when the plan
  is live again;
- **a dead pod** — its claimed rows go back to the queue after 10 minutes
  (the reaper, canon T18 col 11).

**Refused at schedule** (422, with every reason): a workflow whose first door
is keyed "one run per <field>" (a broadcast has no <field> to give), and a
workflow whose sends, action args or playbook lines ask for a fact a
broadcast run never carries (it carries `phone` and `customer_name`) — those
would skip or park everyone.

## Pause, resume, the report, close (phase 4)

`POST /broadcasts/{id}/pause?merchant_id=` holds a dispatching broadcast: the
person being admitted finishes, and the rest of the page goes back to the
queue at once (not after the reaper's lease). `POST /broadcasts/{id}/resume`
admits the rest from where it stopped. Runs already started carry on by their
own rules either way — a broadcast never owns them (canon/06-outreach.md:14-16).
409 when the broadcast is not dispatching (pause) or not paused (resume); a
scheduled broadcast is re-aimed or cancelled instead. Cancel works from
`paused` too.

`GET /broadcasts/{id}/funnel?merchant_id=` is the broadcast's report
(`outreach/broadcast_report.py`; canon/06-outreach.md:45-46 "Funnel read:
skips on T18; sends/refusals/receipts on the runs' manifest rows — runs WHERE
source_broadcast_id = B"; ADR 0010 "the funnel reads calls via the stamped
enrollment id"):

```json
{ "status": "sent", "dispatched_at": "2026-10-20T04:30:00Z",
  "closes_at": "2026-10-24T04:31:10Z", "closed_at": null,
  "recipients": { "frozen": 2600,
                  "by_status": { "enrolled": 2395, "skipped": 205 },
                  "skipped_by_reason": { "no_phone": 100, "already_in_flow": 50,
                                         "cooldown_active": 50, "customer_gone": 5 } },
  "runs": { "started": 2395, "open": { "waiting": 400 },
            "by_exit_reason": { "goal_met": 310, "timed_out": 1600, "completed": 85 } },
  "calls": { "placed": 2380, "answered": 1500, "by_outcome": { "...": 0 },
             "contacted_runs": 2380, "reached_runs": 1500, "...": "..." },
  "computed_at": "2026-10-21T09:00:00Z" }
```

- The workflow's own report counts every run of the plan (from events and
  from every broadcast that launched it); this one counts only this launch's.
- Calls are the same lead reads and the same fold as the plan's calls
  summary (`analytics.calls_of_runs`), so the two never count a run apart.
  They are read in batches of 20,000 runs: a broadcast is not bounded by a
  window the way the Performance tab is. Every read is on the replica.
- **Not counted yet: the runs' WhatsApp messages** (sends, refusals,
  receipts on `crm_message`, T16). The canon's funnel includes them, but the
  manifest has no index on the run that sent a message (056), and today's
  broadcasts call. Adding them is one index on `crm_message (merchant_id,
  source_id)` plus a connectivity read — the follow-up when the first
  WhatsApp broadcast is planned.

**Closing** (canon T17 col 11: "closed = the reporting window ended and the
funnel went final"). The window is the broadcast's **last run's whole life**
(the workflow's `max_age_days`) after that run entered, plus **one day** for
the last call reports. The corpus gives no length; its one example — "fired
Friday 10:00, final Sunday" (T17 col 18) — is a time after the fire, never
"when every run has ended": a one-call plan's runs end when the call is
QUEUED (hours before the dialler rings), and one parked run would keep the
broadcast open forever. The broadcaster's loop runs the close job every
minute beside the reaper; a broadcast that admitted nobody closes at once.
`closes_at` in the report says when.

## Example

[`final-nudge.json`](final-nudge.json): the Diwali season's badge, and a
draft broadcast that will admit "Offered, not granted" into a final-nudge
call workflow. Both bodies are validated in CI against the request models.
