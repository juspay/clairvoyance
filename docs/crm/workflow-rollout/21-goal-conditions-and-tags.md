# Phase 21 — Goal conditions (`goal.where`) and Shopify order tags as a list

**Kind**: feat (no migration) · **PR title**: `feat(crm): conditions on a goal tier` · **Depends on**: 06 (goal tiers + key), 15 (doors), 18 (match) — all merged · **Touches**: `app/crm/outreach/{conditions,entry,walker,catalog_laws,plans,schemas}.py`, `app/crm/record/{events,contracts}.py`, `app/crm/record/db/{queries,accessor}.py`, `app/crm/record/extractors/shopify.py`, `docs/crm/plans/cod-confirm-exits.json`

---

## Why

A goal tier says *which topics* end a run and, since phase 06, *which run* a
letter is about (`goal.key`). It cannot say **what the letter has to contain**.

That was fine while every goal topic was a once-per-order fact
(`orders/cancelled`). It breaks on `orders/updated`, which Shopify fires for a
fulfilment, a shipping edit, a note, a refund and a tag write alike — our own
tag writes included. On `release` the only way to write "exit when fulfilled"
is a tier on `orders/updated` with no condition, and that tier ends the run on
the first edit of any kind: the echo of our own `🔄 BUDDY-ATTEMPTED-1` tag.

The asks, in a plan's words:

| Sentence | Goal tier |
|---|---|
| exit when fulfilled | `orders/updated` + key `id` + `fulfillment_state is "fulfilled"` |
| exit when paid | `orders/updated` + key `id` + `payload.financial_status is "paid"` |
| exit when the order carries tag A | `orders/updated` + key `id` + `order_tags includes "A"` |

`fulfillment_state` is our derived field: Shopify's raw `fulfillment_status`
with its null read as `"unfulfilled"`; `"partial"` is not `"fulfilled"`.

## What is built

### 1 — `where` on a goal tier

`WorkflowGoal.where: List[Condition]`, the door's own typed grammar. ANDed, and
ANDed with `key`. Empty = the topic alone ends the run — every document
published before this phase. `goal_tiers()` is unchanged: keyed first; a
`where` does not reorder.

### 2 — one evaluator

`outreach/conditions.py::conditions_match` judges a condition list against one
letter. A door (`entry._where_matches`), the live goal judge
(`entry._end_on_goal`) and the walker's goal re-check all call it, so a door
and a goal cannot mean different things by the same condition.

### 3 — order tags as a list (no new type, no new op)

The where-grammar is closed (design/event-catalog.md), and its `list` row is
`includes`, `exists`, `not exists`. Shopify REST posts tags as ONE comma
string (`"Buddy Confirmed, COD"`), GraphQL as an array. The Shopify decoder
declares `order_tags` — a derived `list`, beside `fulfillment_state` — that
reads both shapes as trimmed, non-blank tags. `payload.tags` stays the `text`
variable it is on `release`.

| Sentence | Conditions (ANDed) |
|---|---|
| has tag A | `order_tags includes "A"` |
| has A and B | `order_tags includes "A"` + `order_tags includes "B"` |
| has no tags | `order_tags not_exists` |

Matching is exact, as `includes` is everywhere: `"VIP"` never matches
`"Non-VIP"`. "Lacks tag A" has no op (`excludes` would need its own corpus
entry) and is out of scope.

### 4 — the walker re-checks a conditioned tier at fire time

The law: goals are re-checked at fire time (canon/06-outreach.md:125-126,
modules/05-outreach.md:46). The live consumer judges only runs that exist when
the letter is consumed. A founding letter consumed late (a failed consume
retried, or Shopify delivering topics out of order — it guarantees no order
across topics) means the paid or fulfilled letter was judged when no run
existed. Only the walker can still see it.

The EXISTS that serves a plain tier knows topic and key, never the payload, so
for a conditioned tier it would say yes to any edit of this order. Instead, for
a tier with a `where`, the walker reads **one** letter — the newest of the
tier's topics about this order, after the founding letter
(`record.contracts.latest_customer_event`: the EXISTS's own WHERE,
`ORDER BY COALESCE(occurred_at, received_at) DESC LIMIT 1`) — and judges it
with `conditions_match`. Every `orders/updated` carries the whole order, so the
newest letter about one order is that order's state now. One row and one
payload per conditioned tier per claim, on the same index the EXISTS uses — not
the many-payload read rejected in September. A tier without a `where` keeps the
EXISTS exactly as it is.

### 5 — the publish laws

`catalog_laws.goals_against_catalog`, called from `validate_definition`:

- **A tier with a `where` needs a `key`.** The walker judges the newest letter
  of the tier's topics; without a key it may be about another of her orders.
- **`key.event` is a top-level payload field.** Both judges read it at the top
  level only — `entry.py` as `payload.get(key)`, the walker in SQL as
  `payload->>$5`. `payload.id` is refused, not stripped. These two laws need no
  catalog, so a pure caller (`catalogs=None`) meets them too.
- **A conditioned tier's topics must be in the catalog**, with the door's
  sentence: *"register its schema (or declare it in code) before filtering on
  it"*.
- **Each condition goes through `condition_against_catalog`**: declared field,
  op fits type, choice values, deprecation. A tier that names several topics
  must have its `where` hold on **every** topic it names: each condition is
  checked against each topic's fields.
- **`includes` on a list needs non-blank text.** `order_tags` holds trimmed,
  non-blank strings and `1` never equals `"1"`: such a tier would never fire,
  and we would keep calling.

`gather_catalogs` reads the catalog only for tiers **with** a `where`, in both
spellings (`goals`, and the pre-phase-06 `goal`; version rows are immutable).
A plain tier may name any topic, as on `release`.

## What changes for an existing plan

- **At run time, nothing.** No published document carries a goal `where`; a
  tier without one is judged exactly as on `release`.
- **At publish, one new refusal**: a goal `key.event` with a dot
  (`"payload.id"`). Such a tier never matched on `release` either (both
  judges read the top level), so it is a silent bug made loud — but a live
  plan carrying one can no longer be re-published until the key is fixed.
  Check before deploying:

  ```sql
  SELECT w.merchant_id, w.id, w.name, w.status, d.which, g->'key'->>'event' AS key_event
  FROM crm_workflow w,
       LATERAL (VALUES ('definition', w.definition), ('draft', w.draft)) d(which, doc),
       jsonb_array_elements(COALESCE(d.doc->'goals',
                                     jsonb_build_array(d.doc->'goal'))) g
  WHERE w.status <> 'archived' AND d.doc IS NOT NULL
    AND g->'key'->>'event' LIKE '%.%';
  ```

## Rules an author meets

- **One tier per exit reason** (phase 06, unchanged). "Paid or fulfilled" is
  two tiers, so both cannot be `goal_met`. Until a corpus ruling, give the
  second tier a reason of its own — knowing that `converted_elsewhere` means
  "another order by the customer" (canon/06-outreach.md:144), so labelling
  "paid" with it miscounts the funnel. Raised for Swaroop.
- **`orders/paid` is declared but not relayed.** Nautilus relays only
  `orders/create`, `orders/updated`, `orders/cancelled` and `checkouts/update`;
  the Breeze Buddy app subscribes to no `orders/paid`. A goal on it publishes
  and never fires. Write "exit when paid" as `orders/updated` +
  `payload.financial_status is "paid"`.
- **The plan's own tag write echoes back.** A goal on `order_tags includes X`
  where the plan itself writes X ends the run on our own write. Right when the
  sentence is "the confirmation is recorded in Shopify"; wrong when it is
  "someone else confirmed it". Shopify's webhook carries no trace of our
  write, so this is the author's choice.
- **`orders/updated` reaches only relayed shops** (`CRM_EVENT_RELAY_MERCHANTS`,
  or `*`). Unset means no shop.

The worked plan is `docs/crm/plans/cod-confirm-exits.json`: COD door, hold →
call → wait 1d, `goal_met` when fulfilled, `withdrawn` when cancelled.
`test_plan_templates` validates it through every law on every CI run.

## What this does not need

- **No migration.** The goal lives in the definition jsonb; no new
  `exit_reason`, no new table.
- **No nautilus change.** `orders/updated` relay (nautilus #205) merged on
  9 Sep; the `("shopify", "orders/updated")` catalog entry is on `release`
  since #1108.
- **No change to pinned runs.** A run pinned to an earlier version keeps its
  own goals (ADR 0023).

## Tests

`tests/crm/test_workflow_goal_conditions.py`:

- vocabulary: a tier carries `where`; empty is the old behaviour; tiers stay
  keyed-first;
- publish laws, called through `validate_definition`: undeclared topic, wrong
  op, dotted key (with and without a catalog), missing key, blank or non-text
  `includes` value;
- live consumer: the tag edit ends the run; a shipping edit on the same order
  ends nothing; the tag on another order ends nothing;
- walker: a letter consumed before the run existed ends it `goal_met` at the
  first claim; a newest letter that no longer matches ends nothing; no letter
  ends nothing; a plain tier still asks only the EXISTS;
- gather: both goal spellings are read; a plain tier is not gathered;
- evaluator: an array path reads as the door reads it; `order_tags` splits
  both Shopify shapes.

`tests/crm/test_workflow_queries.py` pins the one-row read;
`tests/crm/test_plan_templates.py` pins `cod-confirm-exits.json`.

## Rollout

1. Run the dotted-key query above; fix any plan it returns.
2. Deploy. **Publish plans with a goal `where` only after every outreach pod
   runs this phase.** Code without it ignores the unknown `where` key: the tier
   becomes a plain one and ends runs on our own tag echo. A revert does the
   same to plans already published.
3. Confirm the pilot shop is relayed and its `orders/updated` rows land
   processed with a `customer_id`.
4. Re-publish the pilot plan with the goal `where`. New runs take it; runs in
   flight keep their pinned version (`pin`), or are `migrate`d deliberately.

## Out of scope

- A condition whose value references a run fact (`{"run": …}`).
- A SQL compiler for the predicate (segments' phase).
- `goalable`: declared on every catalog entry, read by nothing — the natural
  reader would refuse a goal on `orders/paid` at publish.
- `where` on a listening square (`wait` with `topics`).
- `excludes` ("lacks tag X").
