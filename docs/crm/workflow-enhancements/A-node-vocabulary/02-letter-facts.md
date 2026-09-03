# A/02 — Letter facts: list-shaped payload data reaches templates (G4)

**Track A · step 2** · **Kind**: feat · **PR title**: `feat(crm): send and call nodes read scalar summaries out of the source letter at fire time (enh A/02)` · **Depends on**: A/01 merged (shares `nodes.py`/`schemas.py`; no logical dependency) · **Notes**: §16.3 G4, §12 (manas's question 1), §3 record contracts

## Why
`entry._context_from_payload` keeps scalars ≤256 chars only (canon T20: context is pointers plus small facts, never payload photocopies). So `line_items`, `addresses`, any list or object never reaches a template: the cart nudge cannot name the items, the loan call cannot read the offer. The letter itself is stored verbatim on `crm_event_raw`; the run holds its id (`source_event_id`, and after rollout 16 each stage's event id under `facts.<node>`). Read it at fire time, once, through record's contract — the "fire-time read" option manas asked about, chosen over producer-owed summaries (every producer would need teaching) and per-plan extractors (code per merchant).

## Design
- Record contract: `event_payload(merchant_id, event_id) -> Optional[Dict]` in `record/events.py`, exported from `record/contracts.py`; accessor `get_event_payload` (single statement, merchant-scoped, `crm_event_raw_customer_ix` not needed — PK lookup).
- Node vocabulary: `send` and `call` nodes gain optional `letter_facts: Dict[str, LetterFact]` where `LetterFact = {path: str, join: str = ", ", max_chars: int = 200, from: "entry" | "<node id>" = "entry"}`. `path` is a deliberately tiny grammar, validated by regex: `a.b.c` for a scalar, `a[*].b` for "the field b of every element of list a" (one `[*]` at most). `from` names which letter: the founding event or a stage's event (rollout 16 records `facts.<node>.source_event_id`).
- Execution (`nodes.execute_send` / `execute_call`): before building variables, if the node has `letter_facts`, fetch the payload(s) once, resolve each path with a PURE `extract_letter_fact(payload, spec) -> Optional[str]` in `outreach/letters.py` (lists joined, truncated at `max_chars` with an ellipsis, non-scalars dropped, missing → None), and merge the results into the variables/lead payload under the given keys — **not** into the stored context (no photocopies; fire time only). A missing letter (retention sweep, or a replayed spine) is NOT a park: the variable is simply absent and the send/call proceeds; log at warning.
- Validator: path grammar; `from` names a node or "entry"; key names do not collide with bookkeeping keys.

## Red tests
- `extract_letter_fact`: scalar path; list path joined; truncation; missing; non-scalar elements skipped; nested `[*]` twice refused by the validator.
- `execute_send` (monkeypatched contract): variables carry `items` when the letter has line items; carry nothing when the letter is gone; the stored context is unchanged.

## Acceptance
- Suite green; boundary clean (outreach → record contracts only). Cart plan template (rollout 07) gains `letter_facts: {"items": {"path": "line_items[*].title"}}` on the WhatsApp node; runbook updated.

## Decisions already made
- Fire-time read through the contract; no producer-owed summary, no per-plan extractor. Letter absence degrades, never parks.
