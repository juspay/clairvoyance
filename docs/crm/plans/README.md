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
| `loan-dropoff/0N-*.json` | Loan-onboarding drop-off (§13 Option A) | one **clock** per stage: wait 30m → call; see that folder's README |

Placeholders: every `template_id` is the string `TEMPLATE_ID_PLACEHOLDER`
so the document validates as-is — replace it with the merchant's Breeze
Buddy template id before publishing (the walker parks a run whose call
node names a template that does not exist). The WhatsApp `template` is a
NAME (`cart_recovery_1`) resolved against the merchant's approved
templates at send time; phase 08 makes publish refuse an unknown one.

Not in the documents yet, on purpose: `on_publish` (`pin` | `migrate`)
is phase 11's word — the cart board will declare `migrate` then (runs are
a day long; fixing a template name should reach every waiting run), the
loan board `pin`.
