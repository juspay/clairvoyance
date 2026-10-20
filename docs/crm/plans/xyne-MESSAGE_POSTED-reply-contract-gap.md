# Finding: the async `MESSAGE_POSTED` reply never reaches a workflow run

**Status:** verified (read-only proof + guardrail test), recommendation only — the provider
face was NOT changed. The real Xyne reply contract is not in-tree, so teaching the parser a
reply shape would be a guess (finding 7 remains open, deliberately).
**Scope investigated:** `app/crm/connectivity/providers/xyne/inbound.py`,
`app/crm/connectivity/ingress.py`, `app/crm/record/extractors/xyne.py`,
`app/crm/outreach/entry.py`, `app/crm/outreach/schemas.py`,
`docs/crm/plans/xyne-postmessage-{run,http-wait}.json`,
`tests/crm/test_plan_templates.py`, `tests/crm/test_xyne_webhook_flow.py`.
**New regression test (applied, passing):** `test_xyne_inbound.py::
test_the_claimed_flat_reply_yields_nothing_and_raises_nothing`.

---

## 1. The gap, in one sentence

The workflow plan contract (`test_plan_templates.py:202-242`) and the runnable companion plan
(`xyne-postmessage-run.json`) believe an async reply is a **flat** body — `eventType`
`MESSAGE_POSTED`, a `messageId`, and `conversationId` at the **top level**. But the Xyne
provider face (`inbound.py:letters -> _parse`) only files letters that carry
`context.flowJSON.data.merchant_id` **and** `context.messageId`. The flat reply has neither, so
`_parse` returns `None`, `letters()` returns `[]`, and the reply is dropped at the door — it
can never reach `_end_on_goal`, no matter how the goal key is written.

The goal-key discrimination test hand-feeds the `conversationId` to the goal, so it passes while
the real plumbing would drop the very body it asserts on. **The test proves the goal grammar,
not the end-to-end reply path.**

## 2. Proof (read-only, `uv run`)

`/tmp/verify_xyne_reply_gap.py` (removed after use; rerun by pasting the body below):

```
[1] inbound.letters(flat reply):
        -> 0 letter(s)
[2] inbound.letters(real submission fixture)  # control
        -> 1 letter(s), owner='flow-shop' ext='e9be4e96-9e96-4c9e-b259-ee75ea8bb499'
```

The parse of the **submission** succeeds (control), the flat **reply** files nothing.

## 3. The full trace — where a callback does and does not go

For a **flow_action submission** (what the form sends on submit):
`verify_signature` (raw HMAC over the body) → `letters()` → `_parse()` reads
`context.flowJSON.data.merchant_id` (owner) + `context.messageId` (external id) → a
`ProviderLetter` naming `owner_id=merchant_id` → `xyne_envelope()` →
`_xyne_merchant_for()` checks that merchant owns a `merchant_http`/`xyne` door → filed as a
`flow_action` `EventIn` with the whole body as `payload` → the extractor
(`extractors/xyne.py`) resolves `payload.context.flowJSON.data.customer_mobile_number` as a
**phone identity** (`about="customer"`) → `consume_attributed_event` gets a `customer_id` →
open runs for that customer are matched → `_end_on_goal` reads `event.payload.get(key.event)`
with the goal key `conversationId` … which the submission nests under `context`, so a keyed
goal skips it.

For a **flat `MESSAGE_POSTED` reply**, the same chain dead-ends **three layers up**:

| Layer | What it needs | The flat reply has | Result |
|-------|---------------|--------------------|--------|
| `inbound._parse` | `context.flowJSON.data.merchant_id` && `context.messageId` | neither — plain top-level fields | **no letter** |
| `_xyne_merchant_for` | a merchant to verify owns the door | no owner to verify | would be dropped even if a letter existed |
| `extractors/xyne.py` | `customer_mobile_number` (the only identity), `about="customer"` | none (topic isn't even the declared `flow_action`) | `customer_id=None` → `entry.py:131` returns |

So even if every attribution layer were satisfied, the reply is a **merchant-scope** event with
no customer, and `consume_attributed_event` returns at the first line — "not about a person:
nothing to admit, end or wake". The `xyne-postmessage-run.json` plan enters on `message.inbound`
(a Meta value object) and keys the goal on top-level `conversationId`; a Xyne reply would not
even be on that topic, and the attribution path (`reply.replied_to` → manifest, see
`docs/crm/reply-run-matching.md`) has no Xyne `replied_to` declared.

## 4. Why this is NOT a one-line parse fix

The task forbids weakening tenant isolation: **do not infer merchant ownership from an
untrusted `conversationId`**, and do not accept a callback without sufficient trusted
attribution. A flat reply's only candidate owner is the top-level thread id — provider-owned
data we never wrote, which is precisely the untrusted thing the current design refuses to read
(`inbound.py` reads the merchant only from `context.flowJSON.data.merchant_id`, "the value the
sending plan and the walker write into the form — never free-typed"). Accepting it would file
under a merchant whose door never sent the form.

## 5. What evidence is missing (finding 7, concretely)

No **captured** Xyne `postMessage` reply body exists in this repo — no fixture, no vendored
guide excerpt, no log sample. The flat shape in `test_plan_templates.py` was authored in the
original commit (`git show 4691ee71`) as a hand-drawn discriminator so the goal-key grammar
could distinguish a "reply" (top-level) from a "submission" (nested); the repository removed
the one test that pinned a standalone reply-like drop. To close the door we need, from Xyne's
own docs or a live harness, the actual response to `POST /api/apps/chat/postMessage`:

1. Does the `MESSAGE_POSTED` event carry `context` at all? Which of
   `context.flowJSON`, `context.messageId`, `context.userId` arrive on a reply vs. a submit?
2. Which field names the owning merchant on the reply (is `flowJSON.data.merchant_id` echoed,
   or is there NO owner field — making replies un-attributable by design)?
3. Which field names the customer/person answered (does the reply echo
   `customer_mobile_number` or a `userId` we can join to a customer)?
4. What `eventType` value(s) a reply uses, and whether it is a distinct topic from `flow_action`.

Without these, **any** reply parser is speculative.

## 6. Proposed patch (NOT applied — gated on the evidence in section 5)

The minimal, isolation-preserving shape **if** a verified reply carries
`context.flowJSON.data.merchant_id` + `context.messageId` exactly like the submission:
both the submission and the reply parse through the `_parse` the face already owns — a reply
then files as a `flow_action` letter naming a merchant from data WE wrote, preserving isolation.

If instead the reply truly is flat (no `context.flowJSON`), the honest conclusion is that a
`flow_action` entry can never hear its own `postMessage` reply — the blocker-finding's
**Option C** applies: drive the run from a customer-level entry and use the Xyne door only as
the outbound action. The reply then never needs to enter on `flow_action`.

Either way, the plan's `event=conversationId, run=conversationId` goal key stays valid for any
topic that a future, verified reply carries `conversationId` on.

## 7. What was changed (this finding)

- **Applied:** `tests/crm/test_xyne_inbound.py` — added
  `test_the_claimed_flat_reply_yields_nothing_and_raises_nothing`, which pins the **current,
  correct** behaviour: a flat `eventType`/`messageId`/`conversationId` body files **no** letter,
  i.e. the face refuses to guess an owner or customer from a thread id. If a verified contract
  arrives, that test must be updated to the real, captured body — it intentionally fails closed
  today.
- **Not applied:** no change to `inbound.py` (no speculative parser), no change to the extractor,
  no change to tenant-isolation logic, no plan JSON edits.

## 8. Test results (exact)

- `uv run pytest tests/crm/test_xyne_inbound.py … test_plan_templates.py … test_workflow_entry.py
  test_ingress_door.py -q` → **115 passed** (114 + the new guardrail).
- `uv run pytest tests/crm/ -q --ignore=test_ingress_integration.py` → **1673 passed, 9 skipped
  (DB-gated)**, 0 failed.
- `uv run pyrefly check` → **0 errors**.
- `uv run black/ isort/ autoflake --check tests/crm/test_xyne_inbound.py` → clean.
- DB-backed integration (`test_ingress_integration.py`) requires Postgres; runs in CI's Postgres
  job.