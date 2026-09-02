# Phase 25 — Dry run: simulate a plan against a sample event (G10)

**Kind**: feat · **PR title**: `feat(crm): POST /workflows/{id}/simulate — walk a document with a fake clock and no writes` · **Depends on**: 17, 20, 21 · **Notes**: §16.3 G10 · **Wave 6**

## Design
- `POST /crm/workflows/{id}/simulate?merchant_id` body: `{event: {topic, payload}, answers?: {<wait_event node>: <label>}, facts?: {...}, use_draft?: bool}` → walks the draft (or live) document PURELY: entry match + admission (assume fresh customer), `_first_wake`, then node by node: waits advance the fake clock; `wait_event` takes `answers[node]` or `timeout`; `condition`/`split` evaluate for real (split with a fixed seed); `send`/`call`/`http`/`handoff` are NOT executed — the response records what WOULD fire with resolved variables (letter facts from the sample payload). Stops at exit or after 50 steps.
- Response: `{admitted: bool, reason, path: [{node, type, at: "+00:30", action: {...}|null}], exit: {reason, at}, problems: [...]}`.
- Implementation: a `outreach/simulate.py` concern that reuses `plans.validate_definition`, `ladder.expand_stages`, `predicates.evaluate`, `nodes.run_facts`, `letters.extract_letter_fact` — every one already pure. Node specs gain an optional `describe(run, node, definition) -> Dict` used only here (send/call/http/handoff implement it; waits do not need it).
- No DB writes; the only reads are the workflow row and, if `letter_facts` name `from: entry`, the sample payload itself (never a real letter).

## Red tests
- The cart plan simulated with a checkout event and no answers → path wait/send/wait/call/wait/completed with the right offsets; with `facts.cart_value` above the tiered threshold → the call arm; loan ladder with a skipped stage → the labelled jump; a plan with problems → 422 with the validator list.

## Decisions already made
- Simulation never touches providers or the spine. Split uses a fixed seed so the response is reproducible.
