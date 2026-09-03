# Runbook — cart recovery (the board in `docs/crm/plans/cart-recovery.json`)

What it does: a Shopify checkout update starts a run; 30 minutes of
silence → WhatsApp nudge; 30 more → rescue call; one more day → the run
completes. An order for **that cart** ends the run as `goal_met` at any
point; any other order by the customer ends it as `converted_elsewhere`.
Every repeated checkout update inside the window restarts the 30-minute
timer and refreshes the facts the nudge will carry.

Paths below carry no `crm` segment on purpose: the CRM routers mount at the app root (ADR 0022 keeps the internal name off every external surface; `app/crm` and the `crm_*` tables keep it inside).

## Before publishing

1. **Worker pods are running** — three `CRM_ROLE`s, or nothing moves:
   `event-worker` (attributes letters, starts/ends runs), `walker` (fires
   waits, queues sends, creates calls), `dispatcher` (delivers queued
   WhatsApp rows). Each needs a DB pool ceiling ≥ 2.
2. **WhatsApp connector onboarded** for the merchant and `healthy`:
   `POST /connectors/whatsapp/onboard?merchant_id=<m>` with the
   Embedded Signup `code` and `waba_id`; check
   `GET /connectors/installations?merchant_id=<m>`. A `degraded`
   door (webhook subscribe failed) cannot send.
3. **The WhatsApp template exists and is approved** under the name the
   plan uses (`cart_recovery_1`):
   `POST /connectors/templates` (body: `merchant_id`, `channel:
   "whatsapp"`, `provider_account_ref`, `name`, `language`, `components`)
   then `POST /connectors/templates/{id}/submit`. Confirm with
   `GET /connectors/templates?merchant_id=<m>&channel=whatsapp` —
   status must be `approved`, in exactly ONE language. Until the Meta
   status webhook (PR #1040) lands, approval is not recorded
   automatically: check with the connectivity owner. A send against an
   unapproved template is `blocked / template_not_approved`; the run
   still continues to the call.
4. **A Breeze Buddy call template** for the rescue call, belonging to the
   merchant (or global), with a `call_execution_config`. Its id replaces
   `TEMPLATE_ID_PLACEHOLDER`. Calls obey buddy's own calling hours,
   DND and blacklist — the walker only creates the lead.
5. **The relay pushes the three topics with these exact strings** —
   `checkouts/update`, `orders/create`, `orders/paid` — as
   `source: "shopify"` to `POST /ingest/events`, Shopify's body
   unopened, with `occurred_at` (Shopify's `updated_at`/`created_at`)
   and an `external_id` that is unique **per delivery**, e.g.
   `checkouts/update:<checkout id>:<updated_at>`. An `external_id` that
   repeats across updates dedupes them at the door and the repeat/debounce
   words never see them.
6. **Auth in hand:** an admin JWT for the `/workflows` routes; the
   relay's wildcard RBAC JWT (Nautilus's `CLAIRVOYANCE_JWT_TOKEN`) or the
   merchant's `s2s_token` for the ingest door.
7. **The goal key is `cart_token`.** Shopify's checkout and order bodies
   both carry it. On the first real payload confirm it is present; if the
   relay ever strips it, switch both `key.event` and `key.run` to `token`.

## Publish

```bash
export BASE=https://<clairvoyance>; export M=<merchant_id>
export H="Authorization: Bearer $ADMIN_JWT"; export J="Content-Type: application/json"

# 1. create (born as a draft; validated at the door -> 422 lists every problem)
jq -n --arg name cart-recovery --slurpfile d docs/crm/plans/cart-recovery.json \
  '{name: $name, definition: $d[0]}' \
  | curl -sS -X POST "$BASE/workflows?merchant_id=$M" -H "$H" -H "$J" -d @-
# -> 201 {"id": "<wf>", "status": "draft", "version": 0, ...}

# 2. (edit the draft again if needed — same body, replaces the draft)
curl -sS -X PUT "$BASE/workflows/<wf>/draft?merchant_id=$M" -H "$H" -H "$J" -d @body.json

# 3. publish: draft -> definition, version 1, status live
curl -sS -X POST "$BASE/workflows/<wf>/publish?merchant_id=$M" -H "$H"

# 4. status (live <-> paused; archived is terminal and ejects open runs)
curl -sS -X POST "$BASE/workflows/<wf>/status?merchant_id=$M" -H "$H" -H "$J" \
  -d '{"status": "paused"}'
```

Publishing again later: put the new document in `draft`, then `publish`.
**Runs finish on the version they entered under** (ADR 0023): a publish
makes version N+1 for new checkouts, and every run already in flight
keeps executing the version N it started on — the walker reads each
run's pinned document, never the live one. To reach the runs in flight
too (a template name fixed, a delay shortened), declare
`"on_publish": "migrate"` in the document: then the publish re-pins every
open run to N+1, and the validator refuses removing a node they stand on
or changing the `entry` words — pause the plan and let them finish, or
publish the change as a new plan.

To push a fix to runs already in flight on a `pin` plan (a wrong template
id on the call node, say): publish the fixed document as version N+1, then
move them — `POST /workflows/<wf>/versions/N/migrate?merchant_id=$M&to=N+1`
(admin). It answers how many moved, and refuses (422) when N+1 drops a
square those runs stand on or changes the `entry`.
`GET /workflows/<wf>/versions?merchant_id=$M` lists every version with the
open runs still executing it; versions are kept for the life of the plan,
so an exited run's version always says what it executed. A WhatsApp
template cannot be retired (409) while an open run's version still names
it, or while a live or paused plan's latest document does — let the runs
finish or migrate them, and republish the plan without it.

## Watch it run

```bash
curl -sS "$BASE/workflows/<wf>/runs?merchant_id=$M&status=waiting" -H "$H"
curl -sS "$BASE/workflows/<wf>/runs?merchant_id=$M&status=parked" -H "$H"   # the triage view
curl -sS "$BASE/workflows/<wf>/runs?merchant_id=$M&status=exited" -H "$H"
```

A run shows `current_node`, `wake_at` (the next alarm — also the walker's
lease while a visit is in progress), `attempts`, `last_error`, and
`context`: the customer's small facts (`cart_token`, `total_price`, …),
the founding letter's id and time (`source_event_id`,
`entered_event_at`), the normalized `phone`, and per-node results
(`message_wa-nudge`, `lead_rescue-call`). `repeat_event_ids` lists the
checkout updates that refreshed the run.

**Parked runs** (`status=parked`) stopped on a deterministic failure and
wait for a human; `last_error` says why:

| `last_error` | Fix, then resume |
|---|---|
| `… no phone in run context` | the checkout had no phone we could read (email-only checkout); nothing to fix — archive or ignore |
| `call node …: template … not found` / `no call_execution_config` | replace the placeholder / configure the buddy template |
| `send node …: <reason>` | the channel/address was refused at queue time (bad number) |
| `node X not in definition vN` | drift across an archive/re-create; re-publish with the node or archive the plan |
| `definition vN missing` | the run's pinned version row is gone (should never happen — versions are never deleted); archive the plan or re-create it |
| `attempts exhausted: …` | a transient error kept failing (provider, DB); check the cause |

```bash
curl -sS -X POST "$BASE/workflows/<wf>/runs/<run>/resume?merchant_id=$M" -H "$H"
```

`resume` puts the run back to `waiting` with `wake_at = now` and the
failure counter forgiven; the walker retries the same node on its next
tick. `last_error` stays visible until the next successful step.

## Exit reasons

| `exit_reason` | Meaning |
|---|---|
| `goal_met` | an order carrying the run's `cart_token` — **this cart recovered** |
| `converted_elsewhere` | the customer ordered something else; the nudge stopped, the cart was not recovered |
| `completed` | the whole board ran (nudge, call, one day) with no order |
| `timed_out` | the run outlived `exits.max_age_days` (7) — normally only a parked-then-resumed run |
| `ejected` | the plan was archived while the run was open |
| `withdrawn` | not used by this board (the loan clocks use it) |

Goal comparisons are against the moment the **checkout update happened**
(`entered_event_at`), not when we stored the run — an order that arrived
late but happened after the abandonment still ends the run.

## How to read the summary

```bash
curl -sS "$BASE/workflows/<wf>/summary?merchant_id=$M&since=2026-09-01T00:00:00Z&until=2026-09-08T00:00:00Z" -H "$H"
```

One object for the window (`since`/`until` bound `entered_at`; omit both
for all time):

| Field | Meaning |
|---|---|
| `runs` | runs that started in the window |
| `open.waiting` / `open.parked` | still in flight / stuck for a human |
| `by_exit_reason` | how the finished ones ended (`goal_met` = this cart recovered, `converted_elsewhere`, `completed`, `timed_out`, `ejected`) |
| `median_minutes_to_exit` | median time from the checkout update to the exit, over finished runs |
| `recovered_amount` | sum of the order amount on `goal_met` runs — the order's `total_price` as the relay delivered it, stored on the run when the goal ended it |

Recovery rate = `goal_met / runs` once the window is old enough for every
run to have finished (a day after `until`, for this board).

A customer's runs across every plan, in the order they started:

```bash
curl -sS "$BASE/customers/<customer_id>/runs?merchant_id=$M" -H "$H"
```

## Settings to change per merchant

| Where | Word | Default in the document | Change when |
|---|---|---|---|
| `entry` | `cooldown_hours` | 24 | how soon after an exited run the same customer may be nudged again |
| `entry` | `debounce_minutes` | 30 | how long the customer must go quiet before the timer fires (only extends, never shortens) |
| `entry` | `on_repeat` | `refresh_latest` | `refresh_max(total_price)` to nudge about the biggest of several carts |
| `nodes` | `minutes` on the three waits | 30 / 30 / 1440 | the merchant's cadence |
| `nodes` | `template` on `wa-nudge`, `template_id` on `rescue-call` | `cart_recovery_1` / placeholder | per merchant |
| root | `purpose_key` | `marketing.cart.recovery` | must be a `marketing.*` purpose — the permission gate (phase 19) will require consent for it |
| `exits` | `max_age_days` | 7 | rarely |

Operator knobs (env, all pods): `CRM_WALKER_LEASE_SECONDS` (300),
`CRM_WALKER_MAX_ATTEMPTS` (3), `CRM_RUN_RETENTION_DAYS` (90, exited runs
are deleted after), `CRM_EVENT_MAX_ATTEMPTS` (5, letters whose consumer
keeps failing are quarantined on the spine, not lost).
