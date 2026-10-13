# Dynamic segment rules

A dynamic segment is a stored question (canon T15 col 6, `definition`); who
matches is computed when asked, never stored. `POST /segments` with a
`definition` checks the rule against the merchant's own event catalog and
saves it; the compiler that answers it is segments phase 4. Code: the
format and its shape laws in `app/crm/shared/segment_format.py` (shared,
because record answers the same rule in SQL and may not import outreach);
the catalog laws in `app/crm/outreach/segment_rules.py`.

## The format (v1 — event fields only)

| Shape | Means |
|---|---|
| `{"all": [rule, …]}` | every child holds |
| `{"any": [rule, …]}` | at least one child holds |
| `{"not": rule}` | the child does not hold |
| `{"event": "<topic>", "within_days": N, "at_least": N, "where": […]}` | the customer had this event — in the last N days (optional), at least N times (default 1), each matching `where` (optional: the workflows' `[{field, op, value}]`, unchanged) |

"Never had X" is `{"not": {"event": "X"}}`.

## The laws (refused at save with every problem, located by path)

- The event is one the merchant sends: a registered schema or a code catalog entry.
- Every `where` field is declared for that event; every op fits the field's type; a choice value is one of its values — the same check a workflow door's `where` passes.
- A derived field (computed in code: Shopify `items_count`, WhatsApp `reply`) is refused — a segment is one SQL query over stored events, and a computed value has no stored form.
- Closed shape: no unknown keys, no empty groups, one word per group, nesting at most 8 deep, at most 50 event conditions.

Customer attributes are not rule material in v1; an inferred one never will be
(canon/05-audiences.md:15 — guesses may never decide).

## Examples

| File | The question | Proves |
|---|---|---|
| [`abandoned-checkout-not-ordered.json`](abandoned-checkout-not-ordered.json) | Abandoned a checkout over 2,000 in the last 3 days, and did not order in those 3 days | `all` + `not`, a time window, a number field — validated in CI against the Shopify code catalog |
