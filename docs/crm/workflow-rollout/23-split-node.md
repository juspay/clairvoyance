# Phase 23 — `split` node: deterministic percentage branches (G5 part 3)

**Kind**: feat · **PR title**: `feat(crm): split node — stable percentage branching for experiments` · **Depends on**: 20 · **Notes**: §16.3 G5 · **Wave 6**

## Design
- Node: `{id, type: "split", arms: [{on: "A", percent: 50}, {on: "B", percent: 50}]}`; percents are integers summing to 100; edges labelled with each `on`. `NodeSpec(is_wait=False, branches=True)`.
- Execution is PURE and STABLE: `bucket = int(sha256(f"{run.id}:{node.id}").hexdigest()[:8], 16) % 100`; the arm is the first whose cumulative percent exceeds the bucket. A lease retry lands the same run in the same arm; the same customer in two runs may land differently (correct: the unit is the run). Returns `{reply_<node>: on, split_<node>: on}` — the second key is a FACT (survives reply clearing) so reports can group by arm.
- Validator: ≥2 arms, integers, sum 100, labels distinct and edged.
- Reporting (phase 09's summary): `by_split: {node: {arm: count}}` read from `context->'split_<node>'` — one extra GROUP BY, keep it in this phase.

## Red tests
- Determinism (same inputs → same arm across 1000 calls); distribution over 10k synthetic run ids within ±3% of the percents; validator refusals.

## Decisions already made
- Hash on run id, not customer id: a re-enrolled customer may be re-randomised; experiments are per run. Document it.
