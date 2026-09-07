# A/01 — `condition` node: branch on what is already known

**Track A · step 1** · **Kind**: feat · **PR title**: `feat(crm): condition node — labelled edges chosen by a predicate over run facts and customer facts (enh A/01)` · **Depends on**: nothing (rollout 15/16 are merged) · **Notes**: rollout `../../workflow-rollout/context/reading-notes.md` §16.3 G5, `../../workflow-rollout/context/nits.md` N1

## Why
Every branch the engine can take today is on something that HAPPENS: a `wait_event` reply, a topic (rollout 15), a call or message outcome (rollout 18), a goal tier (rollout 06). There is no square that reads a fact already in hand and picks an edge without waiting: "cart above ₹5,000 → call, else WhatsApp", "KYC tier A → skip the call", "no phone on file → end". It is the first node type every merchant asks for, so it gets a phase rather than a backlog line.

## Vocabulary (schemas.py)
- `WorkflowNode.type` Literal gains `"condition"`. New fields on the node: `rules: List[ConditionRule]` (min 1) where `ConditionRule = {on: str, if: Predicate}`; edges out of the node are labelled with each rule's `on` plus a mandatory `"else"` edge. Rules are evaluated in document order; the first whose predicate holds wins; none → `else`.
- `Predicate` is a dict `{ <field>: <match> }`, ALL entries must hold (AND; OR is two rules). `<match>` is either a scalar (equality — the exact grammar `entry.where` already has) or an object with one operator: `{"eq"|"ne"|"gt"|"gte"|"lt"|"lte"|"in"|"exists": value}`. Comparisons coerce both sides with the finite-number rule from rollout 00's `_as_number`; a non-numeric side makes the rule FALSE, never an error. A missing field makes the rule FALSE. Predicates never raise — the honest fallback is always `else`.
- `<field>` grammar (validator-checked with a regex): `context.<key>` — the run's facts as `run_facts(context)` exposes them (top-level facts, `current_node`, `current_stage`, `repeat_count`); `facts.<node_id>.<key>` — a specific stage's facts (rollout 16); `customer.<column>` — a whitelist on `crm_customer`: `display_name`, `primary_locale`, `timezone`, `has_phone`, `has_email` (the last two derived, never the values — a predicate must not become a way to read a handle into a log); `customer.attributes.<name>` — the WINNING claim for any asserted attribute in `crm_customer.attributes` (the `facts.py` ladder: declared > observed > imported; an inferred-only attribute reads as absent, matching the materialisation rule), e.g. `customer.attributes.gender`, `customer.attributes.tier`. Handle-like attribute names (`phone`, `email`, `igsid`, `shopify_customer_id`, `external_ref`, anything under `_handle_history`) are refused by the validator.

## Registry (nodes.py)
- `NodeSpec` gains `branches: bool` (True for `wait_event` and `condition`, False otherwise). `walker.pick_next` reads `reply_<node>` when `spec.branches`, else the single plain edge — this retires the `node.type != "wait_event"` string match (N1). `entry.py`'s listening check likewise asks the registry for `listens` (True only for `wait_event`) instead of matching the string.
- `NODE_TYPES["condition"] = NodeSpec(validate=_validate_condition, execute=execute_condition, is_wait=False, branches=True)`.
- `execute_condition(run, node, definition)` → gathers `customer_facts` lazily (only if any rule names `customer.`) via a new identity contract `customer_facts(merchant_id, customer_id) -> Optional[CustomerFacts]` (a leaf shape with the five whitelisted columns plus `attributes: Dict[str, Any]` holding each asserted attribute's winning value, computed with `facts._winner` over the assertion history and excluding inferred winners and handle-like names; implemented over the existing `identity/db/accessor.get_customer`), builds `facts = run_facts(run.context)` and the `facts.<node>` map, and returns `{reply_<node.id>: evaluate(node.rules, facts, stage_facts, customer_facts)}`. `evaluate` is PURE and lives in a new `outreach/predicates.py` (also the home for `entry.where` matching — move `_where_matches` there so both grammars are one function).
- Because `is_wait` is False the walker continues in the same visit: condition → the chosen edge → next node, all under one claim. `reply_<node>` is cleared on advance (rollout 15), so a re-entered condition re-evaluates.

## Validator (plans.py + nodes.py)
- Every rule `on` has an edge; an `else` edge exists; no unlabelled edge; labels distinct (the existing wait_event edge rules, generalised to `branches`).
- Operators in the vocabulary; `in` requires a list; `exists` requires a boolean; fields match the grammar; `customer.` fields in the whitelist; `facts.<node>` names a node in the document.

## Red tests
- `tests/crm/test_workflow_predicates.py`: equality, each operator, AND across fields, first-match order, missing field → false, non-numeric comparison → false, NaN → false, `in`/`exists`; `customer.has_phone` derived correctly; `customer.attributes.gender` reads the declared winner over an older observed claim and reads as absent when only inferred; `customer.attributes.phone` refused by the validator; `entry.where` still matches through the shared function.
- `tests/crm/test_workflow_nodes.py`: registry/Literal parity still holds with the new word; `branches` true exactly for wait_event and condition.
- Walker (monkeypatched accessor): a `wait → condition → {call | send}` document takes `call` when `context.cart_value` is 6000 and `send` when 1000, within one visit (one `advance_run` at the end, no intermediate write); missing `cart_value` → `else`.
- Validator: missing `else` edge refused; unknown operator refused; `customer.phone` refused.

## Acceptance
- Suite green; boundary clean (outreach → identity contracts only; no handle values leave identity). `docs/crm/plans/cart-recovery.json` gains an optional example variant `cart-recovery-tiered.json` (call only above a cart value) validated by the phase-07 test. Runbook section "branching on facts".

## Decisions already made
- Predicates fail to `else`, never raise, never park. A parked run for a typo in a predicate would stop covering the customer; the validator catches shape, the walker forgives data.
- Customer facts are the five columns plus asserted attributes' winners; handle VALUES are never readable by a predicate (only `has_phone`/`has_email`), and handle-like attribute names are refused.
- How an attribute gets there is unchanged: `assert_facts()` from an extractor's `facts` (flat extractor `FACT_KEYS` maps producer keys → attribute names; adding `gender` to a producer's map is a one-line code change, vocabulary in code) or from the Shopify extractor. A fact only present on the triggering event needs no attribute at all — it is in `context.<key>` already.
- No `split` (percentage) and no `http` in this phase — A/04 and A/03.

## Out of scope
- `http` action node (A/03), `split` node (A/04), human handoff (track B).
