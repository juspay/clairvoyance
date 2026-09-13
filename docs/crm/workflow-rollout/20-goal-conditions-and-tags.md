# Phase 20 — Goal conditions (`goal.where`), the `tags` field type, and `shopify/orders/updated`

**Kind**: feat (no migration) · **PR title**: `feat(crm): conditions on a goal tier, a tags field type, and the shopify orders/updated spec` · **Depends on**: 06 (goal tiers + key), 15 (doors), 18 (match) — all merged · **Touches**: `app/crm/shared/predicate.py`, `app/crm/record/{schemas,catalog,contracts,db}`, `app/crm/record/extractors/shopify.py`, `app/crm/outreach/{schemas,entry,walker,catalog_laws}.py`

---

## Why

A goal tier says *which topics* end a run and, since phase 06, *which run* a
letter is about (`goal.key`). It cannot say **what the letter has to contain**.

That was fine while every goal topic was a once-per-order fact —
`orders/paid`, `orders/cancelled`. It breaks the moment the goal topic is
`orders/updated`, which Shopify fires for a fulfilment, a shipping edit, a
note, an address correction, a refund, a tag write, a metafield change. A
tier listening on `orders/updated` today ends the run on the *first edit of
any kind*, which is not a goal — it is noise wearing a goal's costume.

The concrete ask: a plan calls a COD customer, an `action` square tags the
order (`update_order` → `tags: ["Buddy Confirmed"]`), and the run should end
`goal_met` **only when Shopify's order data actually carries that tag** —
not when the parcel ships.

Two facts found while reading, both blocking, both worth stating plainly:

1. **`("shopify", "orders/updated")` is not in the code catalog at all.**
   `decode_spec` finds no entry and no registered row, returns `EMPTY_SPEC`,
   and `_extract` falls through to `flat.extract`, which looks only for a
   top-level `customer_mobile_number`. A Shopify order payload has none.
   So `_run_processor` takes the `if not extracted.handles` branch and
   writes `quarantine_event(..., "no_handle")`. **Every relayed
   `orders/updated` is quarantined today**, `customer_id` is never set, and
   `consume_attributed_event` returns on its first line. A goal on that
   topic currently fires for nobody — and any shop already inside
   `CRM_EVENT_RELAY_MERCHANTS` is accumulating quarantine rows for it.
2. **Nautilus needs no change.** `handleBreezeBuddyOrder` already relays
   `orders/updated` verbatim (`relayOrderFact`, gated per-shop), stamped
   with the edit's own `updated_at` both as `occurred_at` and as the
   `externalId` revision suffix, so each edit is a distinct spine row rather
   than a duplicate of the create. The payload it relays is
   `savedOrder.orderData`, and `upsertOrder` does `order_data =
   EXCLUDED.order_data ... RETURNING *` **before** the app handler is
   dispatched — so the relayed body is the *fresh* webhook payload with the
   new tags, not the stored create-time one. `orders/updated` is subscribed
   declaratively in `shopify-apps/breeze-buddy/shopify.app.toml`.

## The shape of the answer

Not a Shopify branch, and not a tag-matching feature. Three separable pieces,
each of which is the generic version of what was asked:

| # | Piece | Generic statement |
|---|---|---|
| 1 | `goal.where` | A goal tier carries the **same typed `where` grammar a door carries**. Any field, any op, any source. |
| 2 | `tags` type + `has_all` / `has_any` / `has_none` | The grammar learns **list-shaped fields** once, in one evaluator. Shopify's comma string and a vendor's JSON array are the same value to it. |
| 3 | the `orders/updated` spec | One `CatalogEntry` + one fixture. The four-part ritual, no new machinery. |

Everything else in this file is the consequence of those three.

---

## Design

### Part 1 — `where` on a goal tier

**`app/crm/outreach/schemas.py`**

```python
class WorkflowGoal(BaseModel):
    topics: List[str] = Field(min_length=1)
    key: Optional[WorkflowGoalKey] = None
    # Phase 20: what the letter must CONTAIN, in the door's own grammar
    # (shared/predicate.py). ANDed, and ANDed with `key`. Empty = the topic
    # alone ends the run, which is every document published before this.
    where: List[Condition] = Field(default_factory=list)
    exit_reason: str = "goal_met"
```

`goal_tiers()` orders by **specificity**, refining the phase-06 rule rather
than replacing it — keyed tiers still come before unkeyed ones, and within
each half a tier that carries a `where` is judged before one that does not:

```python
def goal_tiers(self, topic=None):
    tiers = [t for t in self.goals if topic is None or topic in t.topics]
    return sorted(tiers, key=lambda t: (t.key is None, not t.where))  # stable
```

No document published before this phase carries a `where`, so no existing
plan's judging order changes. That is the reason for refining rather than
re-sorting.

**`entry.py::_end_on_goal`** — the live path. Generalise the existing door
helper to take conditions instead of a door and use it in both places:

```python
def _conditions_match(conditions: List[Condition], event: RawEvent) -> bool:
    derive = derive_for(event.source, event.topic)
    return matches(conditions, lambda path: field_value(event.payload, path, derive))

def _where_matches(door: WorkflowEntry, event: RawEvent) -> bool:   # unchanged callers
    return _conditions_match(door.where, event)
```

and in the tier loop, after the key check and before `cancel_run`:

```python
if tier.where and not _conditions_match(tier.where, event):
    continue  # this letter is not the thing the tier is waiting for
```

One evaluator, two surfaces. A door and a goal that both say
`payload.gateway is COD` cannot disagree about what that means.

**`goal.key.event` — the wart, fixed here because the example plan walks
straight into it.** `_end_on_goal` reads `event.payload.get(tier.key.event)`,
a bare top-level lookup, while `entry.key` goes through
`field_value(canonical_path(...), derive_for(...))`. An author who writes
`key: {event: "payload.id", run: "id"}` — the spelling the catalog and the
console show them — gets silence. But the walker compiles the same key to
`payload->>$5 = $6`, which is top-level-only by construction, so widening
the reader would make the two judging sites disagree.

Resolution, in this phase, as a law rather than a widening:

- `entry.py` accepts the `payload.` prefix on a single-segment path and
  strips it (`payload.id` → `id`), so both spellings read alike;
- `validate_definition` **refuses** a `goal.key.event` naming a nested or
  derived path: *"goal key must be a top-level payload field (it is compared
  in SQL at fire time); `payload.customer.id` is not one."*

### Part 2 — list-shaped fields, in one evaluator

**`app/crm/shared/predicate.py`** (still leaf — imports nothing internal).

```python
Op = Literal["is", "is_not", "in", ">", ">=", "<", "<=", "=", "exists",
             "has_all", "has_any", "has_none"]
LIST_OPS = ("has_all", "has_any", "has_none")
```

`Condition._value_matches_op`: the list ops take a non-empty list of scalars,
exactly as `in` does — reuse that branch.

The normaliser, and the one place either side of a comparison is spelled:

```python
def as_tag_set(value: Any) -> Optional[FrozenSet[str]]:
    """PURE: a list-shaped value as a comparable set, or None when the value
    is not list-shaped at all (so no op has an opinion — the file's standing
    rule for a field that isn't there).

    A LIST is its scalar items. A STRING is split on commas: that is
    Shopify's REST shape ("Buddy Confirmed, VIP"), and a vendor's GraphQL
    shape is the array — one op has to read both or an author would have to
    know which transport carried their order.

    Every item is stripped and CASEFOLDED, on both sides. Shopify treats
    tags case-insensitively and collapses whitespace; a merchant types
    "Buddy Confirmed" into the console and Shopify stores whatever the
    write sent. Comparing exactly would mean a goal that silently never
    fires — the same class of harm as an unnormalised phone matching no
    suppression (shared/normalize.py's law, applied to a filter)."""
```

`evaluate()`:

```python
if op in LIST_OPS:
    have = as_tag_set(actual)
    if have is None:
        return False
    want = set().union(*(as_tag_set(v) or set() for v in condition.value))
    if op == "has_all":  return want <= have
    if op == "has_any":  return bool(want & have)
    return not (want & have)          # has_none
```

**`has_none` is "present and contains none of these", never "not
`has_any`".** A missing `tags` key returns False for all three, because a
field that is absent is not evidence of absence — the file's existing
conservatism, stated for the new ops so nobody has to infer it. An empty
string or empty array *is* present and empty: `has_none` true, `has_all`
false. Pin both in a test.

### Part 3 — the `tags` field type

- `record/schemas.py`: `FieldType = Literal[..., "tags"]`.
- `record/catalog.py`: `OPS_BY_TYPE["tags"] = [*LIST_OPS, EXISTS_OP]`.
  `KEYABLE_TYPES` is unchanged, so a tags field is not keyable and the
  existing law says so for free.
- `validate_registration` (the vendor layer): `tags` carries no `values`
  (already covered by the choice-only rule) and **may not be `variable`** —
  a list is not a scalar, `engine.extract` drops it, and declaring it would
  be a template promise with no reader. Refused at registration with that
  sentence, so a vendor learns it while they are still registering.

### Part 4 — Shopify: the tags field and the `orders/updated` entry

**`record/extractors/shopify.py`** — one field and one entry:

```python
def _order_fields() -> List[CatalogField]:
    return [
        ...,
        _f("payload.tags", "tags", "Order tags"),   # NEW — create/paid/cancelled/updated
        ...
    ]

ENTRIES = [
    ...,
    _entry("orders/updated", "Order updated", _order_fields()),   # NEW
]
```

Putting `payload.tags` on `_order_fields()` rather than only on the new entry
gives every order topic the same field for free — "goal when an order is
*placed* already carrying tag X" is the same author sentence with a different
topic. `_checkout_fields()` is untouched: a checkout has no tags.

The new entry inherits the full person block, which is what fixes finding
(1): `orders/updated` starts resolving to a customer through
`PHONE_PATHS`/`EMAIL_PATHS`/`payload.customer.id` exactly as `orders/create`
does, instead of quarantining as `no_handle`.

**Fixture** `tests/crm/fixtures/shopify/orders_updated.json` — a real
captured body with a populated `"tags": "Buddy Confirmed, COD"`, an
`updated_at` later than `created_at`, and the phone only under
`customer.default_address` (so the fallback chain is exercised on this topic
too). `tests/crm/test_catalog.py` pins every declared path against it, as it
does for the other five.

### Part 5 — the walker's re-check, without a SQL compiler

The walker re-checks goals at every claim (`_advance`), through
`customer_has_event`, which is one `EXISTS` with an optional
`payload->>$5 = $6`. It cannot express `has_all`.

**Decision: do not compile the predicate to SQL in this phase.** Compiling
`is_not`, `=`, the ordering ops and the list ops correctly needs the field's
catalog *type* at the query (numeric casts must be `CASE`-guarded or a
non-numeric row raises), and it produces a second evaluator that can drift
from `predicate.evaluate` — the exact failure `engine.py` was written to
end ("two hand-written readers of one payload drift"). `event-catalog.md`
owes a SQL compiler to **segments**; that phase can land it with the type
information it needs, and this phase must not half-land it.

Instead the walker uses the **same pure evaluator**, over a bounded read:

- `record/db/queries.py` gains `customer_goal_events_query(merchant, customer,
  topics, since, where, limit)` — the existing `EXISTS` body as a
  `SELECT ... ORDER BY COALESCE(occurred_at, received_at) DESC LIMIT $n`,
  on the same index, with the same optional top-level key narrowing.
  `$1` params only; no new predicate reaches SQL.
- `record/contracts.py` re-exports `customer_goal_events(...) -> List[RawEvent]`
  through `events.py` (the `customer_has_event` seam, one function over).
  No new leaf shape: the existing decoder already returns `RawEvent`.
- `walker._advance`:

```python
for tier in definition.goal_tiers():
    where = ...                       # unchanged: the top-level key pair
    if not tier.where:
        hit = await customer_has_event(...)          # unchanged fast path
    else:
        hit = any(
            _conditions_match_payload(tier.where, letter)
            for letter in await customer_goal_events(
                run.merchant_id, str(run.customer_id), tier.topics,
                since, where, GOAL_LETTER_SCAN)
        )
```

`GOAL_LETTER_SCAN = 50`, a module constant in `walker.py` with its reason in
the docstring. **Every plan without a `where` keeps the indexed `EXISTS`
exactly as it is** — zero regression on the existing path, and the row read
happens only for the tiers that asked for it.

The bound is honest and is stated as a limitation, not hidden: if a run
somehow accumulates more than 50 matching letters inside its own window and
the qualifying one is not among the newest 50, the walker misses it. It is
the *belt-and-suspenders* check — `entry.py` already judged that letter live
at arrival, against the full payload, with no bound.

### Part 6 — the catalog laws for a goal (this is what makes it safe)

Today `gather_catalogs` collects **door topics and `wait_event` topics only**.
Goal topics are validated against nothing, so
`where: [{field: "payload.tags", op: "has_all", ...}]` on a topic that
declares no tags field would publish cleanly and silently never fire — the
precise failure the catalog law exists to prevent.

- `catalog_laws.gather_catalogs`: add every `goals[].topics` entry to the
  topic set it gathers.
- `catalog_laws.goals_against_catalog(definition, catalogs)` — new pure
  function beside `entry_against_catalog`, called from `validate_definition`:
  - a tier with a `where` on a topic no layer declares →
    *"topic 'X' is not in the catalog — register its schema (or declare it in
    code) before filtering on it"*, the door's own sentence;
  - each condition through the existing `condition_against_catalog`, which
    already checks declared-field, op-fits-type, choice values, deprecation;
  - the `goal.key.event` top-level law from Part 1.
- `validate_definition` keeps its `catalogs is None → shape laws only`
  contract, so every pure unit caller is unaffected.

### Part 7 (optional, same grammar) — `where` on a `wait_event` square

`wait_event` branches on `key` equality and nothing else. The identical
`where: List[Condition]` on a listening square — "a letter only answers this
square if it also satisfies these conditions" — is the same three lines in
`entry.py::_answer_for` / `_is_about`, entirely in memory, with **no walker
or SQL involvement at all**. It is arguably the better modelling for "wait
until the order carries the tag" (a square you stand on, with a timeout edge)
than a goal.

Land it in this PR only if the diff stays reviewable; otherwise it is phase
21 and this file says so. Do not land it half-way.

---

## What this does *not* need

- **No migration.** The goal document lives in `crm_workflow.definition` /
  `crm_workflow_version.definition` jsonb; no new `exit_reason`, so migration
  063's CHECK is untouched; no new table, so `TABLE_OWNERS` and
  `docs/crm/migrations.md` are unchanged.
- **No nautilus change.** Verified above, end to end. What to *check* before
  relying on it is in Rollout below.
- **No change to pinned runs.** A run pinned to a pre-20 version keeps its
  own goals, judged by its own document (`definitions.py`) — the ADR 0023
  path, working as designed.

---

## The two traps, stated so an author meets them in the doc and not in prod

**1. The plan's own tag write echoes back.** If the plan's `action` square
writes `Buddy Confirmed` and the goal fires on `has_all: ["Buddy Confirmed"]`,
then Shopify's `orders/updated` for *our own write* ends the run. Whether
that is right depends on which sentence the author means:

- *"the run ends when the confirmation is durably recorded in Shopify"* —
  correct, and the echo **is** the acknowledgement. This is the normal case.
- *"the run ends when somebody else confirms the order"* — wrong, and the
  goal must name a tag the plan does not itself write (an ops tag, a
  fulfilment tag), or the check belongs on a `wait_event` square (Part 7)
  rather than on a goal.

We cannot distinguish the two from the payload: Shopify's webhook carries no
trace of the `_run_ref` idempotency key the action sent. So this is an
author's decision, and the console copy for a tags condition should say so
in one line.

**2. `orders/updated` is a high-volume topic.** Every fulfilment, edit,
refund and tag write on every order of every relayed shop now becomes a spine
row (correctly: the `externalId` revision suffix makes each edit distinct, so
they do not collapse). Two consequences to watch, neither blocking: the
`crm_event_raw` growth rate for pilot shops, and the entry-rules consumer
now running per edit — it is `O(open runs for that customer)` and already
bounded, but the pass rate is what to look at first if the worker lags.

---

## The authored plan, end to end

```json
{
  "purpose_key": "order_confirmation",
  "entry": [{ "topic": "orders/create", "start": "hold", "key": "id",
              "where": [{ "field": "payload.gateway", "op": "is", "value": "COD" }] }],
  "nodes": [
    { "id": "hold",   "type": "wait", "minutes": 30 },
    { "id": "call",   "type": "call", "template_id": "…" },
    { "id": "tag_it", "type": "action", "connector": "shopify", "action": "update_order",
      "args": { "order_id": "{id}", "tags": ["Buddy Confirmed"],
                "note": "Confirmed on a Buddy call" } }
  ],
  "edges": [["hold", "call"], ["call", "tag_it"]],
  "goals": [
    { "topics": ["orders/updated"],
      "key":   { "event": "id", "run": "id" },
      "where": [{ "field": "payload.tags", "op": "has_all",
                  "value": ["Buddy Confirmed"] }],
      "exit_reason": "goal_met" },
    { "topics": ["orders/cancelled"], "exit_reason": "withdrawn" }
  ],
  "exits": { "max_age_days": 3 }
}
```

`key` ties the letter to **this** order (`entry.key: "id"` makes the order id
the enrollment key, and `_context_from_payload` copies the top-level `id`
into the run's context, so `run: "id"` resolves). `where` says which edit
counts. Both must hold. The second tier needs neither — a cancellation is
unambiguous — and that asymmetry is the point of the feature.

---

## Red tests

Every one fails on `release`.

**predicate** — `as_tag_set` over `"A, B"`, `["A","B"]`, `""`, `[]`, `None`,
`{"a":1}`, `"  a ,, B  "`; casefold on both sides (`"buddy confirmed"` in the
payload satisfies `has_all: ["Buddy Confirmed"]`); `has_all` / `has_any` /
`has_none` truth tables; **`has_none` on a missing field is False**; the list
ops refuse a scalar value and an empty list at model_validate.

**catalog** — `OPS_BY_TYPE["tags"]` is exactly the list ops plus `exists`;
`with_ops` fills them; a registration declaring `type: "tags"` with
`variable: true` is refused, one declaring it plain is accepted;
`("shopify","orders/updated")` is in `CATALOG`; every path it declares
resolves against `orders_updated.json`; `payload.tags` resolves on
`orders_create.json` too.

**record decode** — `orders/updated` through `_run_processor` resolves a
customer (the finding-(1) regression: on `release` the same fixture
quarantines `no_handle`).

**queries** — `customer_goal_events_query` contains `LIMIT`, `ORDER BY
COALESCE(occurred_at, received_at) DESC`, the same key clause, and `$1`
placeholders only.

**outreach schema** — a tier with `where` validates; `goal_tiers()` order is
`keyed+where, keyed, where, plain`; a pre-20 document (`goal` singular, no
`where`) is unchanged.

**catalog laws** — a goal `where` on an undeclared topic is refused with the
register-the-schema sentence; `has_all` on a `text` field is refused as a
wrong op; `goal.key.event: "payload.customer.id"` is refused; `"payload.id"`
and `"id"` both accepted and read alike.

**entry** (monkeypatched accessor) — an `orders/updated` with the tag ends
the run `goal_met`; one *without* it (a shipping edit on the same order)
ends nothing; one with the tag but **another order's id** ends nothing.

**walker** — a tier with `where`: `customer_goal_events` returns a
non-matching letter → the run does not exit; returns a matching one → exits
with the tier's reason; a tier **without** `where` still calls
`customer_has_event` and never `customer_goal_events` (pins the fast path).

---

## Acceptance

- `uv run black . && uv run isort . --profile black && uv run autoflake … &&
  uv run pyrefly check && uv run pytest tests/ && uv run python
  scripts/check_crm_boundaries.py` — all unpiped, all green.
- `check_migrations.py --base origin/release` clean and reporting **no new
  migration** (a migration in this PR means the design drifted).
- `predicate.py` still imports nothing internal.
- Import-smoke, not just pyrefly: the catalog registry actually carries
  `orders/updated`, and `outreach.plans.validate_definition` actually refuses
  the four new problems, run from a REPL.
- One commit.

## Rollout

1. Merge; nothing changes for an existing plan (no document has a `where`).
2. Confirm the pilot shop is in `CRM_EVENT_RELAY_MERCHANTS` and that
   `orders/updated` rows are landing **processed with a `customer_id`**, not
   quarantined `no_handle` — that flip is the observable proof Part 4 worked.
3. Re-publish the pilot plan with the goal `where`. It takes effect for new
   runs; runs in flight keep their pinned version (`on_publish: "pin"`), or
   `migrate` them deliberately.
4. Watch `crm_event_raw` growth and the event worker's pass rate for the
   first day of `orders/updated` volume.

## Decisions already made

- The check is a **condition list on the goal tier**, not a tags feature and
  not a Shopify branch. Anything a door can say, a goal can now say.
- The tier's `where` and its `key` are **ANDed**. A tier is one sentence.
- **Case-insensitive, whitespace-trimmed tag comparison, both sides.** The
  alternative silently never fires.
- **`has_none` requires the field to be present.** Absence is not evidence.
- **No SQL predicate compiler in this phase.** One evaluator or none; the
  compiler is owed to segments, with the field types it needs.
- The tags condition's *value* is a **literal list** the author writes. It is
  not automatically read from the `action` node's `args.tags` — the tag a
  goal waits for is very often not one this plan writes.
- The echo (Part 6 trap 1) is an **author's decision**, surfaced in console
  copy, never a silent filter.

## Out of scope

- **A goal condition whose value references a run fact** (`{"run":
  "action_tag_it_tagged"}`, generalising `WorkflowGoalKey` to the whole
  grammar). This is what "the tags this run actually wrote" would need when
  the action's tags are `{placeholder}`-resolved rather than literal. Real,
  clearly scoped, and its own phase.
- **The SQL compiler** for the predicate — segments' phase.
- **`goalable`.** `CatalogEntry.goalable` has been declared since the catalog
  landed and has **no reader anywhere**. The natural one is right here ("this
  topic may not be a goal"), but it needs `Catalogs` to carry entry-level
  flags, not just a field map — a contract shape change. Note it in
  `99-backlog.md` with this file as the reason it was noticed.
- **`variable: true` on a tags field** (list-shaped facts in templates) —
  that is backlog item G4, which needs a ruling.
- **Part 7** if it does not fit this PR.

---

## As built (9 Sep 2026)

Landed as specified above, with four deviations worth recording:

1. **`conditions.py` is a new file.** The plan said generalise
   `entry._where_matches` into `_conditions_match` and call it from the
   walker. The walker importing the entry consumer is the wrong edge, so
   the evaluator became its own concern file —
   `app/crm/outreach/conditions.py::conditions_match` — which both judging
   sites import. Same one-evaluator guarantee, no cycle.
2. **The goal key's two spellings live on the model.**
   `WorkflowGoalKey.event_field` (a property in `schemas.py`) strips a
   `payload.` prefix, so `entry.py` and `walker.py` read one key by
   construction; the nested-path refusal is in `goals_against_catalog`.
3. **Two fixtures, not one.** `orders_updated.json` (carrying the tag) and
   `orders_updated_shipping.json` (a fulfilment on the same order). The
   pair *is* the feature: every test that matters asserts against both.
   `test_catalog.py`'s `OPS_BY_TYPE` pin was extended with `tags` — that
   test is the closed-set law, so extending the set edits it deliberately.
4. **Part 7 (`where` on a `wait_event` square) was NOT landed.** The PR was
   already wide. It is phase 21; the grammar and the evaluator it needs are
   both in place, so it is a small file.

Verified: `pytest tests/` 2544 passed / 15 skipped / 1 xfailed · pyrefly 0
errors · `check_crm_boundaries.py` clean · `check_migrations.py --base
origin/release` reports **no new migration**, as the design requires.

Both regression tests were confirmed RED against the unfixed code by
temporarily reverting each judging site in turn — the walker bypass fails
`test_the_walker_does_not_end_a_run_on_an_edit_the_where_declines` and
`..._accepts`; the live-path bypass fails
`test_a_shipping_edit_on_the_same_order_ends_nothing`.

**Still blocked on nautilus.** None of this fires until nautilus #205
(`feat(crm-relay): relay orders/updated, keyed by the edit's own
timestamp`) merges. It is currently applied to the working tree as a
patch, not on `release`.
