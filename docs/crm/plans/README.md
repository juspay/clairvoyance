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
| `loan-dropoff.json` | Loan-onboarding drop-off (§16.2; rollout phase 17) | one **pinned board** written as a `stages` ladder: five stages in order; quiet 30m on a stage (120m on the offer) → call → listen for a day → the end; expanded into the wait_event board at create/draft/publish |

Placeholders: every `template_id` is the string `TEMPLATE_ID_PLACEHOLDER`
so the document validates as-is — replace it with the merchant's Breeze
Buddy template id before publishing (the walker parks a run whose call
node names a template that does not exist). The loan board names it once,
in `stages.on_idle`; a stage may name its own under
`stages.overrides.<topic>.on_idle`. The WhatsApp `template` is a NAME
(`cart_recovery_1`) resolved against the merchant's approved templates;
publish refuses an unknown or unapproved one (phase 08).

`on_publish` (ADR 0023): the loan board declares `pin` — a journey lives
for weeks and finishes on the document it entered under; a fix reaches
the runs in flight only through the migrate-forward route. The cart board
carries no `on_publish` word and so defaults to `pin` too; the notes'
intent for it (`migrate`, §16.1 — runs are a day long, a template fix
should reach every waiting run) is not in the document yet.

The ladder (`stages`, phase 17): `order` is the funnel, `idle_minutes` the
"went quiet on a stage" clock, `on_idle` the action it fires (a call or a
send), `after_action_minutes` the listening window after it,
`restart_on_repeat` whether a retried stage letter re-arms the square the
run stands on, `overrides` a stage's own clocks or action. The expander
(`app/crm/outreach/ladder.py`) gives every stage an `at-`, `act-` and
`after-` square and one labelled arrow to every later stage; the CI test
computes that arrow set from the order and fails when the expansion
differs — one missing arrow is one wrong phone call. The stored document
carries both the ladder and the board it produced; a document may not
draw `nodes`/`edges`/`entry` beside its ladder.
