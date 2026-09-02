# Loan drop-off as clocks — one small plan per stage

The funnel (`loan.profile_created` → `kyc_completed` → `bank_linked` →
`offer_accepted` → `agreement_signed` → `disbursed`) ships as **five
clocks**, not one board (notes §13 Option A, §14.7 sequencing): each
clock is a plan whose entry is one stage topic and whose only work is
"wait 30 minutes, then call".

How five clocks behave as one funnel:

- **Progress closes the clock.** Clock *k*'s goals list **every stage
  after k** plus `loan.disbursed`. The goal-cancel runs before entry in
  the consumer, so one stage letter ends clock *k* (`goal_met` — read it
  as "progressed", not "converted") and opens clock *k+1*.
- **Skipped stages are harmless.** Because every downstream topic is a
  goal, a customer who jumps from KYC straight to an offer closes the KYC
  clock all the same. `tests/crm/test_plan_templates.py` computes each
  clock's goal list from the ordered funnel and fails CI if one topic is
  missing — one missing topic is one wrong phone call.
- **Retries re-arm the clock.** `on_repeat: refresh_latest` +
  `debounce_minutes: 30`: a repeated stage letter (a KYC retry) slides
  the alarm and carries the newest facts to the call.
- **One application, one thread.** `key: application_id` — two
  concurrent applications by one customer run two clocks; admission
  (`reenter`, the 1-hour `cooldown`) is judged per application.
- **Leaving the funnel.** `loan.rejected` / `loan.withdrawn` end any open
  clock as `withdrawn`.
- **Unrelated letters are ignored.** An order or a refund matches no
  entry and no goal.

Cost of the pattern: a journey is up to five short runs, each exiting
`goal_met`; "where do customers drop" is a join of a customer's runs
across the five plans (phase 09 adds that read). **Phase 17 replaces
this folder with one `loan-dropoff.json` board** (`stages` ladder, pinned
versions): publish the board, pause the five clocks, let their open
runs finish (they live 30 minutes), archive them. The stage topics, call
templates and goal lists carry over one-to-one.
