# Runbook — loan drop-off as clocks (`docs/crm/plans/loan-dropoff/`)

What it does: a customer who reaches a stage and then goes quiet for 30
minutes gets a call. One plan ("clock") per stage; the next stage event
closes the current clock and starts the next; skipped stages are fine;
`loan.rejected` / `loan.withdrawn` end everything. Read that folder's
README for why five clocks and not one board (phase 17 changes that).

Paths below carry no `crm` segment on purpose: the CRM routers mount at the app root (ADR 0022 keeps the internal name off every external surface; `app/crm` and the `crm_*` tables keep it inside).

## Before publishing

1. **Worker pods are running** — `event-worker` and `walker` at least
   (no WhatsApp in these plans, so the `dispatcher` is not required).
2. **The lender pushes stage events** to `POST /ingest/events` with
   the relay JWT or the merchant's `s2s_token`:

   ```json
   {
     "merchant_id": "<m>",
     "source": "lender-api",
     "topic": "loan.kyc_completed",
     "external_id": "kyc:APP-1001:2026-09-03T10:15:00Z",
     "occurred_at": "2026-09-03T10:15:00Z",
     "payload": {
       "application_id": "APP-1001",
       "customer_mobile_number": "+919876543210",
       "customer_name": "Ravi",
       "stage": "kyc_completed"
     }
   }
   ```

   Rules that matter: **topic strings must match the plans exactly**
   (`loan.profile_created`, `loan.kyc_completed`, `loan.bank_linked`,
   `loan.offer_accepted`, `loan.agreement_signed`, `loan.disbursed`,
   `loan.rejected`, `loan.withdrawn`); `payload.application_id` is the
   run key (an event without it is refused, not re-keyed);
   `customer_mobile_number` is how the customer is resolved and the
   number the call dials (the default extractor reads the flat shape);
   `occurred_at` is what "went quiet" and "progressed after" are measured
   from — send the lender's own timestamp; `external_id` unique per
   delivery so retries of the same stage still reach the debounce.
3. **A Breeze Buddy call template per stage** (or one stage-aware
   template): replace `TEMPLATE_ID_PLACEHOLDER` in each file. The call
   payload carries the run's facts (`application_id`, `stage`, whatever
   scalars the lender sends) as `{placeholder}` variables.
4. **Auth:** an admin JWT for `/workflows`.

## Publish the five clocks

```bash
export BASE=https://<clairvoyance>; export M=<merchant_id>
export H="Authorization: Bearer $ADMIN_JWT"; export J="Content-Type: application/json"
for f in docs/crm/plans/loan-dropoff/0*.json; do
  name="loan-dropoff-$(basename "$f" .json)"
  jq -n --arg name "$name" --slurpfile d "$f" '{name: $name, definition: $d[0]}' \
    | curl -sS -X POST "$BASE/workflows?merchant_id=$M" -H "$H" -H "$J" -d @-
done
# then, per returned id:
curl -sS -X POST "$BASE/workflows/<wf>/publish?merchant_id=$M" -H "$H"
```

Order does not matter — each clock only listens for its own stage topic.
`GET /workflows?merchant_id=$M` lists them with status and version.

## Operate

- **Pause one stage** without touching the others:
  `POST /workflows/<wf>/status {"status": "paused"}` — its open runs
  snooze (the walker skips them) and no new runs start; `live` resumes.
- **Change a stage's timing or template:** put the new document in
  `draft`, `publish`. Clocks are empty most of the time (runs live ~30
  minutes), so the stranding guard rarely bites; if it does, pause, wait
  half an hour, publish.
- **Parked runs:** `GET /workflows/<wf>/runs?merchant_id=$M&status=parked`.
  Almost always `no phone in run context` (the lender omitted the
  number) or a call template problem — fix, then
  `POST /workflows/<wf>/runs/<run>/resume?merchant_id=$M`.
- **A customer's journey** = their runs across the five plans in stage
  order; until phase 09 adds the read, query each plan's runs and match
  `context.application_id`.

## Exit reasons

| `exit_reason` | Meaning for a clock |
|---|---|
| `goal_met` | **progressed** — a later stage (or `loan.disbursed`) arrived before the alarm; not a conversion by itself |
| `withdrawn` | `loan.rejected` or `loan.withdrawn` arrived |
| `completed` | the alarm fired and the call was placed — the drop-off record |
| `timed_out` | outlived `exits.max_age_days` (1) — a parked run left alone for a day |
| `ejected` | the plan was archived while the run was open |
| `converted_elsewhere` | not used by the clocks |

Progress is judged against the moment the **stage event happened**
(`entered_event_at`): a later stage delivered late still counts if it
happened after; an earlier stage delivered late cannot resurrect a clock.

## Settings to change per merchant

| Word | Default | Change when |
|---|---|---|
| `entry.cooldown_hours` | 1 | how soon the same application may be called again after a clock exits |
| `entry.debounce_minutes` | 30 | the "went quiet" window (a retried stage event re-arms it) |
| `nodes[wait-30m].minutes` | 30 | the idle threshold per stage (the offer stage may deserve longer) |
| `nodes[dropoff-call].template_id` | placeholder | per stage, per merchant |
| `goals[0].topics` | every downstream stage + disbursed | only when the funnel gains a stage — add it to every earlier clock (CI checks the list) |
| `exits.max_age_days` | 1 | rarely |
