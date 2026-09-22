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
   **Map the template's blanks.** A send node posts EXACTLY the facts its
   `variables` map names — `{"1": "customer_name"}` fills positional
   `{{1}}`; a named template maps `{"name": "customer_name"}` — and
   nothing when the map is empty. The plans ship with `{{1}}` ←
   `customer_name`; if your approved `cart_recovery_1` has more blanks
   (or none), edit the map to match before publishing: a count mismatch
   is a terminal provider refusal on every send. Every fact must be a
   declared `variable` field of the entry topic (`GET /catalog`), or
   `current_node` / `current_stage`.
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

## Variant: a fallback after the call (`docs/crm/plans/cart-recovery-fallback.json`)

When the rescue call does not reach her — no answer, busy, an early
hang-up — send a second WhatsApp instead of waiting a day in silence.
The variant is the same board with one listening square after the call
(rollout phase 18, G2):

```json
{"id": "after-call", "type": "wait", "topics": ["call.completed"],
 "key": "outcome", "minutes": 1440,
 "match": {"payload": "enrollment_id", "run": "id"}}
```

with the arrows `NO_ANSWER` / `BUSY` / `EARLY_HANGUP` → `wa-fallback`
(a second template, `cart_recovery_2`) and `else` → `wait-1d`.

- **Where the outcome comes from.** Every lead the walker places carries
  the run's id (`enrollment_id`); when the lead finishes, buddy mirrors
  `call.completed` onto the spine with `enrollment_id` and `outcome`, and
  the consumer wakes the run standing on `after-call` with the outcome
  as its answer.
- **`match` says whose letter it is.** A customer can have two open runs
  (two carts). `match` compares the letter's `enrollment_id` with the
  run's own `id`, so one call's outcome never wakes the other run. The
  run side may also name a context field (`lead_rescue-call`, or
  `message_<node>` for delivery receipts once those letters exist).
- **`else` is the catch-all arrow.** The outcome after a call that
  connected is the buddy template's own word (`CONFIRMED`, `not_found`,
  whatever the template sets) and cannot be listed; `else` takes every
  answer the square did not name — the alarm too, when there is no
  `timeout` arrow — so a connected call still keeps the day of listening
  and an order inside it counts as recovered. The words buddy's
  dispatcher writes itself: `NO_ANSWER`, `BUSY`, `EARLY_HANGUP`,
  `BLACKLISTED`, `PRECHECK_FAILED`, `ABORT`/`ABORTED`, `TRANSFERRED`,
  `UNKNOWN`.
- **An arrow back to a call square IS another call.** Since 10 Sep 2026
  every visit to a call square mints its own lead (`lead_visits_<square>`
  keys the id), so `after-call --NO_ANSWER--> rescue-call` rings again on
  every pass — bounded only by the run's age. Any board that draws such an
  arrow is bounded by `exits.max_calls_per_day` (phase 20) — **only when
  the plan writes it**, counted on `exits.timezone`, which a ceiling must
  name. Nothing fills either word in, at read or at write, so a board that
  does not ask for a bound does not get one; write it on any board that can
  ring again. Past that many calls in a day the call square
  places none, leaves `max_calls` on its trail row, and the run takes its
  normal arrow — no fact is written, because `run.max_calls_reached` is
  COMPUTED from the ledger whenever a rule asks for it. The count starts
  over at midnight on that clock — no sweep and no cron, because the run's
  ledger is stamped with the day it counted. Two things follow. A listening
  wait right after it (`after-call`, 180 minutes on `call.completed` in that
  plan) hears nothing and leaves by its alarm — put a `condition` on
  `run.max_calls_reached` between the two to route past it. Being
  computed, it can be judged anywhere on the board: at 09:00 the next
  morning it reads the fresh allowance, not last night's answer.
  `docs/crm/plans/cart-recovery-retry.json` is that shape. And no outcome
  webhook fires for a call never placed. The ceiling counts the walker's own
  visits; buddy's per-lead re-dials (`call_execution_config.max_retry`) are
  a separate layer — `max_calls_per_day: 3` on a template with
  `max_retry: 2` is up to nine dials in a day.

  **Keep a wait on the way back.** `call → call` and `call → condition →
  call` run under ONE walker claim, so every lead of the loop is minted
  milliseconds apart and the visit is parked as a runaway before its ledger
  ever commits — and the ceiling cannot bound that, because the ledger never
  persists. Publish does not refuse the shape; the `after-call` square is
  what keeps this board out of it.
- **Delivery receipts and STOP** (the message half of phase 18) wait for
  the WhatsApp webhook and extractor PRs (#1040, #1052).

Publish it exactly like the board above; the second template must be
approved on the merchant's WhatsApp account or publish refuses it.

## Branching on facts (`docs/crm/plans/cart-recovery-tiered.json`)

A `condition` square (enh A/01) picks an edge from what the run already
knows — no waiting, no letter. The tiered board calls only when the cart
is worth it and nudges on WhatsApp otherwise:

```json
{
  "id": "decide",
  "type": "condition",
  "rules": [
    { "on": "big", "if": [ { "field": "context.total_price", "op": ">=", "value": 5000 } ] }
  ]
}
```

with edges `["decide", "rescue-call", "big"]` and `["decide", "wa-nudge", "else"]`.

- **Rules are judged in order**; the first whose conditions ALL hold names
  the edge. OR is two rules. None holding takes the mandatory `else` edge —
  a predicate never parks a run.
- **The ops are the door's own where-grammar** (`is`, `is_not`, `in`, `=`,
  `>`, `>=`, `<`, `<=`, `exists`, `not_exists`): `is` compares text exactly,
  `=` and the ordering ops read numbers (Shopify posts money as `"1850.00"`).
  `exists` and `not_exists` take no value: `not_exists` holds only when the
  fact is absent (a letter that cleared it reads absent too).
- **Fields** say where the value comes from: `context.<key>` (the run's
  facts, plus `current_node` / `current_stage`), `facts.<node>.<key>` (one
  stage's letter), `customer.display_name` / `primary_locale` / `timezone`
  / `has_phone` / `has_email`, and `customer.attributes.<name>` (the winning
  claim of an asserted attribute — an inferred-only claim reads as absent).
  A handle value is never readable: `customer.phone` is refused at publish.
- **The customer is read once**, and only when a rule names `customer.`.
- The square is not a wait: the walker evaluates it, takes the edge, runs
  the next action and writes once, all in the same visit.

## Calling hours on a wait (`window`)

A `wait` may carry a window, so its timer only moves the run on
inside those hours:

```json
{ "id": "wait-30m", "type": "wait", "topics": ["checkouts/update"], "key": "$topic",
  "minutes": 30, "window": { "opens": "09:00", "closes": "21:00", "timezone": "Asia/Kolkata" } }
```

- `opens` / `closes` are HH:MM on the `timezone` clock; `closes` is exclusive, and an
  `opens` later than `closes` spans midnight.
- `minutes` may be left out. A wait with only a window waits until the window opens
  ("wait until morning"; at once when it is already open). A listening wait with no
  `minutes` listens for as long as the run may live (`exits.max_age_days`) — and with
  a window too, it still listens for the run's life and only acts inside the hours.
- The window decides when the timer may act, so when a call is QUEUED; the dialler's
  own calling hours decide when the phone rings. A narrow window is unforgiving: a
  duration that overshoots its close by minutes waits until the next day's opening.
- A timer that ends outside the hours holds the run on the square, still listening,
  until the window opens; then it moves on. Inside the hours nothing changes.
- A letter is never held: it moves the run the moment it lands, and a goal ends the
  run at any hour. So publish refuses a plan where a letter arrow from a windowed
  square reaches a `call` without first reaching a waiting square — send the letter
  back to the rule or to another wait.
- **This is a scheduling window on the plan's clock, not the customer's, and not the
  quiet-hours control.** Nothing here knows the customer's timezone: a wrong
  `timezone` calls at the wrong local hour. The dialler's calling hours on the call
  template remain the check on every call.

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
tick. `last_error` stays visible until the next successful step. An
event the parked run's square listens for also resumes it by itself —
the customer moved, so the run is no longer stuck on what parked it —
but that path clears `last_error` at once (the letter is the step that
unstuck it), so the breadcrumb is only kept on a manual resume.

## Exit reasons

| `exit_reason` | Meaning |
|---|---|
| `goal_met` | an order carrying the run's `cart_token` — **this cart recovered** |
| `converted_elsewhere` | the customer ordered something else; the nudge stopped, the cart was not recovered |
| `completed` | the whole board ran (nudge, call, one day) with no order |
| `timed_out` | the run outlived `exits.max_age_days` (7) — normally only a parked-then-resumed run |
| `ejected` | the plan was archived while the run was open |
| `withdrawn` | not used by this board (the loan board uses it) |

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
| `exits` | `max_calls_per_day` | **unset** — no ceiling unless you write one | the calls one run may place in a day. Write it on any board that can ring again; leave it out and the board is bounded only by `max_age_days` |
| `exits` | `timezone` | **unset** — required as soon as you name a ceiling | the clock the day resets on |

Operator knobs (env, all pods): `CRM_WALKER_LEASE_SECONDS` (300),
`CRM_WALKER_MAX_ATTEMPTS` (3), `CRM_RUN_RETENTION_DAYS` (90, exited runs
are deleted after), `CRM_EVENT_MAX_ATTEMPTS` (5, letters whose consumer
keeps failing are quarantined on the spine, not lost).
