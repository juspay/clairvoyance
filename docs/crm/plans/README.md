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
| `loan-dropoff.json` | Loan-onboarding drop-off (§16.2; rollout phase 17) | one **pinned board** written as a `stages` ladder: five stages in order; quiet 30m on a stage (120m on the offer) → call → listen for a day → the end; expanded into the wait_event board at create/draft/publish |

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
