# Runbook — loan drop-off as one board (`docs/crm/plans/loan-dropoff.json`)

What it does: one run per loan application, from the first stage letter
it is seen at until the loan is disbursed, rejected or withdrawn, or the
customer has gone quiet after a call. A customer who reaches a stage and
goes quiet for 30 minutes (120 on the offer stage) gets a call; the board
then listens for a day, and silence after that ends the run as
`completed` — the drop-off record. Progress to ANY later stage moves the
token to that stage's square (skipped stages are fine); a retried stage
letter re-arms whatever square the run stands on.

The document is a `stages` **ladder** (rollout phase 17, notes §16.2):
the five stage topics in order, the clocks and the action stated once.
Create/draft/publish expand it into the wait_event board the walker runs
and store both — `GET /workflows/<wf>` shows `stages` beside the
`nodes`/`edges`/`entry` it produced. Per stage the squares are
`at-<stage>` (listening for every later stage, the idle clock),
`act-<stage>` (the call) and `after-<stage>` (listening again, the
post-call window); `<stage>` is the topic's last segment with `_` as `-`
(`loan.kyc_completed` → `at-kyc-completed`). Versions are pinned
(`on_publish: pin`, ADR 0023): a journey finishes on the document it
entered under.

Paths below carry no `crm` segment on purpose: the CRM routers mount at the app root (ADR 0022 keeps the internal name off every external surface; `app/crm` and the `crm_*` tables keep it inside).

## Before publishing

1. **Worker pods are running** — `event-worker` and `walker` at least
   (no WhatsApp in this plan, so the `dispatcher` is not required).
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

   Rules that matter: **topic strings must match the plan exactly**
   (`loan.profile_created`, `loan.kyc_completed`, `loan.bank_linked`,
   `loan.offer_accepted`, `loan.agreement_signed`, `loan.disbursed`,
   `loan.rejected`, `loan.withdrawn`); `payload.application_id` is the
   run key (an event without it is refused, not re-keyed);
   `customer_mobile_number` is how the customer is resolved and the
   number the call dials (the default extractor reads the flat shape);
   `occurred_at` is what "went quiet" and "progressed after" are measured
   from — send the lender's own timestamp; `external_id` unique per
   delivery so retries of the same stage still reach the debounce.
3. **One stage-aware Breeze Buddy call template**: replace
   `TEMPLATE_ID_PLACEHOLDER` in `stages.on_idle.template_id`. The call
   payload carries the run's facts as `{placeholder}` variables: the
   first letter's scalars overlaid by the most recent stage letter's (the
   latest letter wins — `stage`, `amount`, whatever the lender sent last),
   `current_stage` (the stage topic the run went quiet on —
   `loan.kyc_completed`), `current_node` (`at-kyc-completed`), and every
   letter also under `facts_<square that heard it>_<field>` (the KYC
   letter, heard on the profile square, is `facts_at-profile-created_stage`).
   A stage that needs its own template names it under
   `stages.overrides.<topic>.on_idle`.
4. **Auth:** an admin JWT for `/workflows`.

## Publish the board

```bash
export BASE=https://<clairvoyance>; export M=<merchant_id>
export H="Authorization: Bearer $ADMIN_JWT"; export J="Content-Type: application/json"
jq -n --arg name loan-dropoff --slurpfile d docs/crm/plans/loan-dropoff.json \
  '{name: $name, definition: $d[0]}' \
  | curl -sS -X POST "$BASE/workflows?merchant_id=$M" -H "$H" -H "$J" -d @-
# then, with the returned id (publish takes a draft live):
curl -sS -X POST "$BASE/workflows/<wf>/publish?merchant_id=$M" -H "$H"
```

A 422 on create names the problem: a `stages` document that also carries
`nodes`, `edges` or `entry`, a top-level `debounce_minutes` or
`restart_on_repeat` (the ladder sets both per door), an override for a
topic not in `order`, two stages whose names collide.

## Cutting over from the five clocks

The clocks (`loan-dropoff-0N-*`, phase 07) and the board admit the same
letters, so both live at once would call the same customer twice. A plan
has no "drain" status: `paused` snoozes its open runs (the walker skips
them) and admits nothing; `archived` ejects them.

1. Publish the board (above). It is live from that call.
2. In the same minute, pause every clock:
   `POST /workflows/<clock>/status {"status": "paused"}` × 5.
3. The runs a clock had open at that moment are customers who went quiet
   under 30 minutes before the cutover and are not on the board (the board
   admits on a stage letter). List them first if you want to reach them
   another way: `GET /workflows/<clock>/runs?merchant_id=$M&status=waiting`.
   They snooze under the pause and exit `ejected` at step 4.
4. A day later, archive the clocks:
   `POST /workflows/<clock>/status {"status": "archived"}` × 5.

The stage topics, the call template and the exit words carry over
one-to-one; the reporting join across five plans becomes one run.

## Operate

- **Pause / resume the funnel:** `POST /workflows/<wf>/status`
  `{"status": "paused"}` — open runs snooze (the walker skips them) and no
  new runs start; `live` resumes them where they stood.
- **Change a clock, the template or a stage:** edit `stages` in the
  draft (`PUT /workflows/<wf>/draft` with the whole definition —
  `stages`, `goals`, `exits` and the admission words; do not send the
  expanded `nodes`/`edges`/`entry` back), then `publish`. Journeys in
  flight finish on the version they entered under (`pin`); the new
  version takes the next entrants. To move the open runs too:
  `GET /workflows/<wf>/versions?merchant_id=$M` shows every version with
  its open runs, and
  `POST /workflows/<wf>/versions/<from>/migrate?merchant_id=$M&to=<n>`
  moves them — refused (422) when the target drops a square they stand on
  or changes a door, which adding or renaming a stage does; then let that
  version drain instead (runs live at most `exits.max_age_days`).
- **A retired call template** stays refused (409) while any open run or
  the live document names it — publish the replacement first.
- **Parked runs:** `GET /workflows/<wf>/runs?merchant_id=$M&status=parked`.
  Almost always `no phone in run context` (the lender omitted the
  number) or a call template problem — fix, then
  `POST /workflows/<wf>/runs/<run>/resume?merchant_id=$M`. A stage letter
  the parked run's square listens for moves it by itself (and clears
  `last_error`, unlike the manual resume).
- **A customer's journey:** `GET /customers/<customer_id>/runs?merchant_id=$M`
  — one run per application, `current_node` naming the stage square it
  stands on.

## How to read the funnel

```bash
curl -sS "$BASE/workflows/<wf>/summary?merchant_id=$M&since=<iso>&until=<iso>" -H "$H"
```

| Field | Meaning for the board |
|---|---|
| `runs` | applications first seen (at any stage) in the window |
| `by_exit_reason.goal_met` | disbursed |
| `by_exit_reason.completed` | called and then quiet for a day — the drop-offs |
| `by_exit_reason.withdrawn` | rejected or withdrawn |
| `open.waiting` | still in the funnel |
| `median_minutes_to_exit` | median journey length |

Where they dropped: the runs list (`GET /workflows/<wf>/runs`) — a run's
`current_node` says the stage it stands on (`after-kyc-completed` = called
at KYC, still listening), and `exit_reason: completed` with the last
square says where the journey ended. `recovered_amount` is empty (loan
events carry no order amount).

## Exit reasons

| `exit_reason` | Meaning for the board |
|---|---|
| `goal_met` | `loan.disbursed` arrived — the conversion |
| `withdrawn` | `loan.rejected` or `loan.withdrawn` arrived |
| `completed` | the after-call window passed in silence, or the last stage was called — the drop-off record |
| `timed_out` | outlived `exits.max_age_days` (30) |
| `ejected` | the plan was archived while the run was open |
| `converted_elsewhere` | not used by this board |

Progress is judged against the moment the **stage event happened**
(`occurred_at`): a later stage delivered late still moves the token if it
happened after the square was entered.

## Settings to change per merchant

| Word | Default | Change when |
|---|---|---|
| `stages.idle_minutes` | 30 | the "went quiet" window for every stage |
| `stages.overrides.<topic>.idle_minutes` | offer: 120 | one stage deserves a different window |
| `stages.after_action_minutes` | 1440 | how long to keep listening after the call before closing the journey |
| `stages.on_idle.template_id` | placeholder | per merchant; per stage under `overrides` |
| `stages.restart_on_repeat` | true | set false if a retried stage letter must NOT re-arm the current square |
| `stages.order` | the five stages | the funnel gains or loses a stage — publish as a new version; open runs finish on theirs (the CI test recomputes every arrow) |
| `cooldown_hours` | 1 | how soon the same application may start a new journey after one ends — an hour keeps a stage letter delivered late, after disbursal, from opening a run and calling |
| `exits.max_age_days` | 30 | the longest an onboarding may take |
