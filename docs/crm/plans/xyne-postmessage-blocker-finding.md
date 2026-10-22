# Finding: a `flow_action`-entering workflow cannot run under the current CRM engine

**Status:** verified (read-only proof), recommendation only — no code was changed.
**Scope:** `app/crm/record/extractors/xyne.py`, `app/crm/record/workers.py`,
`app/crm/outreach/entry.py`, `app/database/migrations/058_create_crm_workflow_enrollment.sql` (+ new migration).

---

## 1. The blocker, in one sentence

A plan that **enters or is keyed on `flow_action` can never enrol a run**, because a
`flow_action` letter is declared `about="merchant"` (customer `NULL`), and the outreach
entry consumer returns immediately — "not about a person: nothing to admit, end or wake" —
before any run can be created. No run means no `merchant_http` POST, no goal, and no reply.

This is not a mock-vs-live limitation — it is a contradiction between the plan's premise and
the runtime's own treatment of `flow_action`. Running the demo by **any** means (mock Xyne or
live `spaces.xyne.juspay.net`) would show one outcome: the webhook is accepted/filed, then
**nothing happens**.

## 2. Proof (read-only, `uv run`)

`/tmp/verify_flow_action_blocker.py` — no database, no writes; mirrors the pinned
`test_a_merchant_level_letter_starts_ends_and_wakes_nothing` fail-on-touch double:

```
[1] merchant-level flow_action (customer_id=None): returned, accessors touched = none
[2] the flow_action spec declares about='merchant'; the worker passes customer_id=None
    flow_action entry about = 'merchant'
PASS: a flow_action letter cannot start/end/wake a run under current code.
```

The consumer returns **before touching any DB accessor** (an accessor stub raises if read),
proving no run is reachable from a merchant-level letter.

## 3. The chain that makes it impossible

| Layer | Where | Effect |
|------|-------|--------|
| Spec | `app/crm/record/extractors/xyne.py:95` — `about="merchant"` | The letter is "about the form", not a person |
| Doc | `xyne.py:5-12` — "no binding table from userId to a merchant customer… resolution is impossible by design" | A deliberate spec decision |
| Worker | `workers.py:231-232` — `if not customer_id and extracted.about == ABOUT_MERCHANT: return _Processed(None, …)` | stamped `customer_id=NULL`, never resolved |
| Consumer | `entry.py:131-132` — `if customer_id is None: return` | the one line that makes it a hard stop |
| Enrol | `enrol.py` `enrol(customer_id: str, …)` | requires a customer id |
| Schema | `migrations/058` — `customer_id uuid NOT NULL` | **no merchant-level run concept exists at the DB** |
| Pinned test | `tests/crm/test_workflow_entry.py:1124` — "starts, ends and wakes nothing" | the contract enshrines the bail-out |

Even a forced customer id would not help: `flow_action` decodes with **no handles** and the
enrolment-open-unique + `open_runs_for_customer` are keyed by customer — there is no
"merchant-form-level run" to attach the run to.

## 4. Paths to fix (pick one)

### Option A — make `flow_action` customer-able (the "real run" fix)
This requires answering **who the run belongs to** first. The payload gives only
`context.userId` (the platform's filler id) with no binding table to a merchant customer.

1. **Decide the identity.** Either
   - add a `xyne_user_customer` binding table (userId → customer_id) so the extractor can
     resolve; or
   - treat `context.userId` (or `context.conversationId`) as the run key directly.
2. `xyne.py`: change `about="merchant"` → `about="customer"`, declare the identity path.
3. `test_xyne_extractor.py:64,71` — flip `about == "merchant"` assertions.
4. `workers.py` will then resolve() the letter; the handle/identity must be declared for that.
5. `entry.py` now receives a `customer_id` and the plan's normal entry→goal cycle runs.
6. No schema change needed (customer-keyed runs exist); only the binding table or new identity.

### Option B — add a merchant-level run concept (large)
`customer_id NOT NULL` → nullable + `merchant_scope` + open-unique over merchant+workflow,
plus `consume_attributed_event` must stop returning on `customer_id is None` and instead
route merchant letters to a merchant-scope enrole path. This contradicts `test_a_merchant_level_letter_starts_ends_and_wakes_nothing` and the "outreach begins where a customer does" doctrine — the bigger lift, and probably wrong.

### Option C — keep `flow_action` merchant-level; re-scope the demo
The plan premise is the problem. If the goal is to prove the `postMessage → MESSAGE_POSTED →
goal_met` cycle, drive it from a **customer-level** entry instead (a WhatsApp `button.reply` or
`orders/create` letter), and use the Xyne door only as the outbound action. Then `flow_action`
is genuinely merchant-level (fine as-is) and the run is customer-keyed (fine as-is).

## 5. Recommendation

**Option C** for the immediate demo proof (customer-level entry → `postMessage` → reply →
goal), with **Option A** only if the product truly needs a run keyed to a form filler. Option B
is the wrong shape for the codebase's doctrines.

## 6. Note for whoever owns the earlier plan JSON

`docs/crm/plans/xyne-postmessage-http-wait.json` validates and its discriminator logic is
internally consistent, but it enters on `flow_action`, so **publishing it succeeds and it will
simply never enrol** — a silent no-op, invisible in a happy-path trace. The plan needs a
customer-level entry door to be runnable.

## 7. Proof-of-runner

`/tmp/verify_flow_action_blocker.py` (read-only; `uv run python /tmp/verify_flow_action_blocker.py`).
Not committed; rerun anytime to reconfirm after any of the above changes.