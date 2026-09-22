# Plan templates — the two target flows as documents CI validates

Each `*.json` here is the `definition` body of `POST /workflows`
(wrap it as `{"name": "...", "definition": <file>}`; the runbooks in
`../runbooks/` show the exact calls). `tests/crm/test_plan_templates.py`
validates every file on every CI run and pins the shape the notes
decided, so a vocabulary change that would break a published flow fails
the PR that makes it.

| File | Flow | Shape |
|---|---|---|
| `cart-recovery.json` | Cart abandonment (`context/reading-notes.md` §16.1) | one board: wait 30m → WhatsApp → wait 30m → rescue call → wait 1d |
| `cart-recovery-fallback.json` | The cart board with a fallback after the call (rollout phase 18, G2) | after the rescue call, a listening square hears THIS run's `call.completed` (`match` on `enrollment_id`): no answer / busy / early hang-up → a second WhatsApp; `else` → the day of listening |
| `cod-confirm.json` | COD confirmation, and the reply that answers ONE send (`../reply-run-matching.md`) | send → a listening square → tag the order: `CONFIRM` / `form_submitted` → confirmed, `CANCEL` → cancelled, `timeout` → wait. Note what is NOT here: no `match`. Her tap carries the id of the message it answers and the manifest names the run that sent it, so one customer's several open orders keep several separate runs with nothing declared |
| `cart-recovery-tiered.json` | The cart board with a `condition` square (enh A/01) | wait 30m → **decide**: `context.total_price >= 5000` → rescue call, `else` → WhatsApp nudge → wait 1d. One square reads a fact already in hand and picks the labelled edge without waiting |
| `cart-recovery-split.json` | The cart board with a `split` square (enh A/04) | wait 30m → **which-letter**: 70% `control` → `cart_recovery_1`, 30% `variant` → `cart_recovery_2` → wait 1d. One square sends a fixed share of runs down each arm and remembers which arm each run took, so the plan's summary reports runs per arm (`by_split`) and the two letters can be compared. The arm is a hash of the run id and the square id, so a lease retry never moves a run between arms; the shares total 100 because a split has no `else` |
| `line-nudge.json` | A lending journey on a merchant's own registered events, with a `condition` on a fact rendered from inside an array | LINE_INITIATED → **is-credit-line**: `context.products not_exists` AND `context.offers exists` → quiet 30m within the calling window 07:00–23:00 IST (every non-terminal event re-arms it and re-checks) → call → listen 30d; `else` → listen. `offers` exists only when the topic's `item_where` kept at least one credit line with an offer, and then carries only those lines, numbered, each with its lender and offer id. `not_exists` holds only when the fact is absent: a customer whose letter carries products takes `else`. The `window` on quiet-30m holds a timer that ends outside the hours until 07:00, still listening; a letter is never held |
| `line-nudge-mobile.json` | The lending journey gated at the DOOR on a value inside an array (§The `list` ruling) | `LINE_INITIATED` where `payload.products.sub_category includes "Mobile"` → the line-nudge ladder. The door asks the list its one question against the raw array, with the value written in the plan: any product whose sub_category is Mobile admits the customer, a basket without one never starts a run (a `condition` would enrol everyone and only withhold the call), and a one-product basket sent as a bare object is a list of one. A list admits `includes` / `exists` / `not_exists` at the door and nothing else — `is`/`in` compare a field's one value, and a list has many |
| `line-nudge-playbook.json` | The lending journey with a **playbook**: the plan chooses the agent's words (corpus `modules/05-outreach.md` §The playbook, canon T19 col 6) | The agent template keeps `{hook_line}`, `{walk}` and `{lender_notes}` as holes; `playbook.lines` holds every sentence ONCE and `playbook.blocks` name lines via `when`/`say`, first match wins with a mandatory default — the condition square's `else`, and what keeps the agent from speaking a literal `{hook_line}`. The call square LISTS what it takes (`blocks`), so a block nobody asks for is never evaluated, never in a payload, never in a log. Because the playbook rides the document it is pinned into the version row at publish: a journey started on script v1 keeps saying v1, and a wording fix reaches open runs only with `on_publish: migrate` — the property a live vendor registration cannot have (the 17 Sep ruling). `walk` renders many lines, so publish refuses it mapped to a WhatsApp blank while a call takes it happily |
| `loan-dropoff.json` | Loan-onboarding drop-off (§16.2; rollout phase 17) | one **pinned board** written as a `stages` ladder: five stages in order; quiet 30m on a stage (120m on the offer) → call → listen for a day → the end; expanded into the wait board at create/draft/publish |
| `line-nudge-call-letters.json` | The lending journey whose call squares wait for their call **and hear the merchant** (22 Sep 2026; `../runbooks/awaiting-call.md`) | Each call square says `"event_name": "call.completed"` and lists the merchant's topics with `match` and arrows back to the rule: a letter while the call is queued aborts that call and re-decides on the new facts (a ringing call finishes; its late report yields to the letter); the report takes the plain edge with the outcome on the step; the backstop aborts the unplaced call. INVALID_PHONE / BLACKLISTED end the run as `ejected`; a run that ends takes its queued calls with it |
| `line-nudge-call-wait.json` | The lending journey whose call squares **wait for their call** (22 Sep 2026; `../runbooks/awaiting-call.md`) | Each call square says `"event_name": "call.completed"`: it queues its lead and holds the token until that call's own report lands (matched on the lead id it queued), then takes its one plain edge with the call's outcome on the step — a run never has two calls queued, and the gap after a call counts from the call's END. `await_minutes` is the backstop. Without `event_name` a call square queues and moves on at once, as before |

Placeholders: every `template_id` is the string `TEMPLATE_ID_PLACEHOLDER`
so the document validates as-is — replace it with the merchant's Breeze
Buddy template id before publishing (the walker parks a run whose call
node names a template that does not exist). The loan board names it once,
in `stages.on_idle`; a stage may name its own under
`stages.overrides.<topic>.on_idle`. The WhatsApp `template` is a NAME
(`cart_recovery_1`) resolved against the merchant's approved templates;
publish refuses an unknown or unapproved one (phase 08).

A send node's `variables` map (`{"1": "customer_name"}`) is the ONLY
thing posted to the provider as template fill-ins — one entry per blank,
the fact a declared `variable` field of the entry topic (or
`current_node` / `current_stage`, or a listened square's
`facts_<square>_<key>`). The cart boards map `{{1}}` ← `customer_name`;
edit the map to the merchant's approved template before publishing (the
runbook's step 3). `test_plan_templates` validates every board against the
CODE catalog (Shopify's declared fields); the loan board's `loan.*` topics
are a vendor's, so the test supplies the registration the vendor signs at
enrollment (`payload.application_id`, keyable) — publish on a real
merchant needs that same `POST /ingest/schemas` first.

`on_publish` (ADR 0023): the loan board declares `pin` — a journey lives
for weeks and finishes on the document it entered under; a fix reaches
the runs in flight only through the migrate-forward route. The cart
boards declare `migrate` (§16.1): runs are a day long, so a template fix
should reach every waiting run — and the stranding validator still guards
every such publish (an occupied square may not vanish, the entry may not
change while runs are open).

The ladder (`stages`, phase 17): `order` is the funnel, `idle_minutes` the
"went quiet on a stage" clock, `on_idle` the action it fires (a call or a
send), `after_action_minutes` the listening window after it,
`restart_on_repeat` whether a retried stage letter re-arms the square the
run stands on, `overrides` a stage's own clocks or action. The expander
(`app/crm/outreach/ladder.py`) gives every stage an `at-`, `act-` and
`after-` square and one labelled arrow to every later stage; the CI test
computes that arrow set from the order and fails when the expansion
differs — one missing arrow is one wrong phone call. A keyed ladder
(top-level `key`) gives every listening square `match` on that key, so
two applications of one customer never move on each other's letters
(phase 18). The stored document
carries both the ladder and the board it produced; a document may not
draw `nodes`/`edges`/`entry` beside its ladder.
