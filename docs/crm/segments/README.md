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
- A date-time field is not compared (`>`, `<`) yet — Postgres 14 has no safe cast, and one malformed date in one payload would fail the merchant's whole query. `exists` / `not_exists` work; recency is the event's own `within_days`.

Customer attributes are not rule material in v1; an inferred one never will be
(canon/05-audiences.md:15 — guesses may never decide).

## How a rule is answered (segments P4)

Outreach owns the segment; **record answers the rule**, because the events
are record's table (`crm_event_raw`) and cross-module reads go through
contract functions. `record.contracts.customers_matching(merchant_id, rule,
after, limit)` returns one page of customer ids (pass the last id back as
`after`); `count_matching(merchant_id, rule)` returns how many. Both re-check
the rule first (`record/segments.py`) and read the replica, like the
console's reports.

The routes (outreach):

| Route | Answers |
|---|---|
| `GET /segments/{id}/members?merchant_id=&after=&limit=` | one page, in customer_id order: a static list's rows, or whoever a dynamic segment's rule holds for right now (`added_at` empty — nobody added them) · 404 · 422 a stored rule record cannot answer · 503 the answer could not be read in time (try again) |
| `POST /segments/{id}/count?merchant_id=` | the segment with `member_count` + `computed_at`: a dynamic segment counted now and stamped with the moment it was asked (the stamp only moves forward); a static list's exact count as it stands · 404 · 422 · 503 the count could not be read in time — the detail carries the count that still stands, with its `computed_at`, so the console keeps showing it |

`computed_at` is the moment the count was asked. The replica that answers it
runs a few seconds behind the primary, so the number is as of a moment
slightly earlier than its stamp.

The rule becomes ONE grouped query (`record/db/queries_segments.py`): the
merchant's events, grouped per customer, kept when the rule's tree holds
over one count per event condition. A `where` condition means exactly what
it means at a workflow door — `tests/crm/test_segment_parity.py` runs the SQL
beside the Python evaluator over a grid of payloads and every op.

Who is counted (v1):

- A customer is the `customer_id` stamped on the merchant's events. A merge
  is not followed yet: events stamped before a merge still count under the
  merged-away id. The follow-up reads across the pair at query time
  (canon/01-identity.md: `id = $2 OR merged_into_id = $2`), never by
  re-stamping events — a merge is undone with one UPDATE, a re-stamp is not.
- A rule that holds with no events ("never ordered" — `{"not": {"event":
  "orders/create"}}`) is answered over everyone who has sent the merchant at
  least one event. Someone known only from an uploaded list is in no dynamic
  segment; a static list holds them.

## Examples

| File | The question | Proves |
|---|---|---|
| [`abandoned-checkout-not-ordered.json`](abandoned-checkout-not-ordered.json) | Abandoned a checkout over 2,000 in the last 3 days, and did not order in those 3 days | `all` + `not`, a time window, a number field — validated in CI against the Shopify code catalog |
