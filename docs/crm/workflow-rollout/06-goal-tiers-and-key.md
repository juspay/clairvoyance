# Phase 06 — Goal tiers, goal key, occurred_at guard (cart→order attribution, G7)

**Kind**: feat + migration · **PR title**: `feat(crm): goal tiers with a payload key — recovered vs converted elsewhere, time-aware on the entry event` · **Depends on**: 01, 03 · **Notes**: §12 Q2/Q3, §16.1, §16.3 G7

## Why
Today `goal.topics` is customer-level: ANY order by the customer ends every open run as `goal_met` (`entry.py` goal loop → `cancel_open_runs`; `walker._advance` → `record.contracts.customer_has_event`). For cart recovery that is the right SAFETY default (never nudge someone who just bought) but it cannot say whether THIS cart was recovered. The funnel needs: order carrying the run's cart → `goal_met`; any other order → still exit, different reason.

## Design
### Vocabulary (schemas.py)
- `WorkflowGoal` becomes a tier: `{topics: [...], key: Optional[{event: str, run: str}], exit_reason: str = "goal_met"}`.
- `WorkflowDefinition.goals: List[WorkflowGoal]` (min 1). Keep `goal` (singular) accepted for backward compatibility: a `model_validator(mode="before")` rewrites `{"goal": X}` → `{"goals": [X]}`. Validator: at most one tier per exit_reason; `exit_reason` ∈ the closed set (see migration); `key.run` must be a context field name (string), `key.event` a payload field.
- Matching order: tiers are evaluated **in document order**; the first tier whose topic matches AND whose key matches (or has no key) wins. So `[{recovered, key}, {converted_elsewhere}]` gives the two-tier cart semantics; a single tier without a key is today's behaviour.
### Migration `NNN_extend_crm_workflow_enrollment_exit_reasons.sql`
- `ALTER TABLE crm_workflow_enrollment DROP CONSTRAINT crm_workflow_enrollment_exit_reason_check` (058 declared the CHECK inline on the column; Postgres names it `<table>_<column>_check` — verify with `\d crm_workflow_enrollment` and fall back to a `DO` block over `pg_constraint` if the name differs), then `ADD CONSTRAINT crm_workflow_enrollment_exit_reason_ck CHECK (exit_reason IN ('goal_met','timed_out','withdrawn','ejected','completed','converted_elsewhere'))`. Closed status enum → CHECK is REQUIRED (law 11). Header cites T20 and this phase; canon amendment proposed to Swaroop.
### Record contract
- `record/contracts.py::customer_has_event(merchant, customer, topics, since, where: Optional[Tuple[str, str]] = None)` → `db/queries.py::customer_has_event_query` adds `AND payload->>$5 = $6` when given. `record/events.py` threads it. This is a read on record's own table through its own contract — no boundary issue.
### Outreach
- `db/queries.py::cancel_open_runs_query` gains optional `(run_field, event_value)` → `AND context->>$6 = $7`. `exit_reason` already parameterised.
- `entry.py`: build `goal_matches` as `(flow, tier)` pairs; for each open-run-relevant tier call `cancel_open_runs(..., tier.exit_reason, event.occurred_at, key=(tier.key.run, payload[tier.key.event]) if tier.key else None)`. Tier order = document order; stop at the first tier that cancelled ≥1 run **per run** — simplest correct implementation: evaluate keyed tiers first (they are more specific) then unkeyed; a run already exited by tier 1 is not matched by tier 2 because the WHERE has `status <> 'exited'`. Document this in the loop.
- `walker._advance`: iterate tiers; call `customer_has_event(..., since, where=(tier.key.event, run.context.get(tier.key.run)) if tier.key else None)`; exit with the tier's reason.
### G7 — time-aware on the entry event, not `entered_at`
- `enrol.py::_enrol_in_txn`: store `context["entered_event_at"] = <source event occurred_at or received_at ISO>` (entry.py passes it in context; it is bookkeeping → add to `nodes._BOOKKEEPING_KEYS`).
- Goal comparisons use `COALESCE((context->>'entered_event_at')::timestamptz, entered_at)` in `cancel_open_runs_query` and pass the same value as `since` in the walker. A late-delivered earlier-stage event can no longer keep a run alive past a goal that truly happened after it.

## Red tests
- Schema: `{"goal": {...}}` still validates and yields one tier; two tiers with the same exit_reason refused; unknown exit_reason refused.
- Queries: `cancel_open_runs_query` with key contains `context->>$6 = $7`; `customer_has_event_query` with where contains `payload->>$5 = $6`; both contain the `entered_event_at` COALESCE.
- Entry (monkeypatched accessor): order with matching `cart_token` → cancel called with `goal_met` and the key; non-matching → `converted_elsewhere` without key.
- Walker: two-tier definition, `customer_has_event` returns True only for the keyed where → exits `goal_met`; False/True → `converted_elsewhere`.

## Acceptance
- Suite green; boundary clean; `check_migrations --base` clean; `TABLE_OWNERS` unchanged.
- `docs/crm/migrations.md`: note the exit_reason set now includes `converted_elsewhere` and why.
- §16.1 cart definition in the notes is now valid as written (`goals` list).

## Decisions already made
- Two tiers, not item-overlap logic (not expressible; §12 Q3).
- Exit reason is a closed set in a CHECK (law 11), so a migration is required.
- Cart key defaults in the plan template (phase 07) to `cart_token`; confirm the relay payload carries it.

## Out of scope
- Recovered-revenue capture at goal time (phase 09).
