# Flagship: COD ship-confidence

## The problem it attacks

Return-to-origin on cash-on-delivery orders is the single largest avoidable cost for
COD-heavy merchants on Breeze. Every RTO costs forward and reverse shipping, working
capital, and often the sale. The platform already touches each COD order one to three
times before it ships: a WhatsApp confirmation (`docs/crm/plans/cod-confirm.json`), a
Breeze Buddy confirmation call, and sometimes an address exchange. Today each touch
collapses into one word, `CONFIRMED`, `CANCEL`, `COD_CONFIRMED`, chosen by a generative
model or an exact string match, and a "confirmed" order that was confirmed hesitantly
by a relative who could not repeat the address ships exactly like one confirmed firmly
by the buyer.

The signal to separate those two is already in the transcript and the reply. Nobody
extracts it, because extracting it needed a generative model per interaction and a
parser for its prose.

## The idea

For every COD order journey, produce a calibrated **ship-confidence band** with
**reason flags** and a **recommended action**, computed from the interactions the
platform already had, and hand it to the merchant's plan as a fact it can branch on.

Jev's role is narrow and exactly its shape: turn each interaction into a **typed feature
vector** in one request. A small model or rule table in code combines the features with
the things code already knows (order value, pincode serviceability, repeat-customer
history, prior RTO rate for the area) and picks a band. The plan decides what to do
with the band.

```
interaction text ──► Jev: ~15 typed questions ──► features ──► code: band + action ──► spine letter
                                                     ▲
                       code-owned facts: value, pincode, history, area RTO rate
```

## Feature questions

All in one request per interaction. The state is the transcript or the reply plus the
merchant message it answered, with phone and email removed.

**From the confirmation call transcript**

```
final_position         choice{confirmed, cancelled, address_updated, callback_later, wrong_number, undecided}
confirmation_firmness  score[refused, hesitant, agreed_when_pushed, agreed, eager]
address_read_back_ok   noul   the customer confirmed the address as read back
address_changed        noul
asked_delivery_date    noul
mentioned_travel_or_absence  noul
non_committal_phrases  noul   "dekhte hain", "baad mein batata hoon", "shayad"
answered_by_other      choice{customer, relative_or_colleague, stranger}
asked_to_cancel_then_flipped  noul
price_objection        noul
comprehension_trouble  score[none, some, heavy]
annoyance              score[calm, irritated, angry]
```

**From the WhatsApp reply**

```
reply_class      choice{confirm, cancel, address_change, question, wrong_number, stop, unclear}
```

**From the shipping address text**

```
completeness     score[junk, area_only, street_no_house, complete]
looks_like_placeholder  noul   "test", "asdf", repeated digits
```

Everything numeric stays in code: order value bands, distance, delivery SLA, days since
last order, the customer's historical RTO count, the pincode's RTO rate. Jev sees none
of it and is asked none of it.

## The band and the action

Code maps the features to a band. Start with a transparent rule table, graduate to a
logistic model once a few thousand labelled outcomes exist.

| Band | Typical evidence | Recommended action |
|---|---|---|
| `high` | firm confirmation by the customer, address read back, complete address | `SHIP` |
| `medium` | hesitant, or relative answered, or asked about dates, address fine | `SHIP`, flag for the merchant |
| `low` | non-committal phrases, address incomplete or changed without read-back, price objection | `RECONFIRM` via a second WhatsApp or `NUDGE_PREPAID` with a small discount |
| `very_low` | cancel-then-flip, stranger answered, placeholder address | `HOLD_FOR_REVIEW` |
| `unknown` | no interaction reached a decision | treat as today |

The band is a **string fact** on a new spine letter, `order.ship_confidence`, with the
raw feature answers stored on the judgment row, not on the letter. Plans branch on it
with the sealed grammar as it stands:

```json
{"type": "condition", "rules": [
  {"when": [{"field": "facts.ship_confidence.band", "op": "is", "value": "low"}], "then": "reconfirm"},
  {"when": [{"field": "facts.ship_confidence.band", "op": "is", "value": "very_low"}], "then": "hold"}
], "else": "ship"}
```

No new operator, no new node word, no numeric comparison in the plan. A `condition`
square reads a fact; an `action` square tags the Shopify order (`add_tag` already
exists) or calls the merchant's endpoint; a `send` square runs the reconfirmation.

## Why this is the best use case for this model in this codebase

- **It needs calibration, not generation.** The value is in probabilities that mean what
  they say, so that `hesitant` at 0.55 and `hesitant` at 0.95 are treated differently.
  That is the one thing this model does that the current stack does not.
- **It needs 100% coverage.** A sample cannot hold an order. At about $0.0002 per
  interaction the whole COD volume of the platform is a rounding error.
- **It needs many questions per interaction.** One request carries the whole feature
  vector; there is no second call whose input depends on the first.
- **Every piece of infrastructure already exists.** The plan grammar, the `condition`
  and `action` nodes, the spine, the post-call worker, the Shopify connector and the
  goal tiers (`orders/cancelled`, delivered) that give us labels for free.
- **It respects every law.** No identity, no attribution, no gate. A new letter, not an
  edit. Fail-open: a missing band ships as today. The only thing that changes is that a
  merchant can choose to act on evidence they never had.

## Measurement

1. **Backtest first.** Take the last N months of COD orders that had a call or a reply
   and a known end state (delivered, RTO, cancelled). Run the feature questions on the
   stored transcripts, fit the rule table, report AUC and the RTO rate per band. This
   costs a few dollars and one day.
2. **Shadow.** Emit the letter for live orders, tag nothing, compare bands with actual
   outcomes for two weeks per merchant.
3. **A/B with the `split` node.** Half the low-band orders get the reconfirmation
   branch, half ship as today. The metric is RTO rate at equal ship volume, and
   secondary, recovered confirmations from the reconfirmation branch.

Success is a measurable RTO reduction on the low band without a fall in shipped volume.
Failure is also informative: if the bands do not separate on real transcripts, the
feature questions are wrong, and the raw answers are stored so they can be re-cut
without re-calling the model.

## Rollout order

1. U2 (post-call verification) ships the worker plumbing and the judgment table.
2. Add the feature questions to the same request. Backtest.
3. U3 (reply classification) supplies the WhatsApp half.
4. Emit `order.ship_confidence`; publish the `condition` example in `docs/crm/plans/`.
5. One pilot merchant on the `split` node.

## Open decisions for the corpus

- Where the judgment table lives (a `crm_*` table with `merchant_id` first, or the
  buddy-side evaluation tables) and its retention.
- Whether `band` is a fact on the run context or a customer attribute; the ladder's
  `inferred` rule suggests it must be run context, never an attribute.
- The exact list of declared outcomes per template that `final_position` should offer.
