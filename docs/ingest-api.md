# Event ingestion API — `POST /ingest/events`

The push door (A9) for services outside this repo: hand a fact to the
event spine over HTTP and get a receipt. Your letter is stored verbatim and attributed to a customer moments
later, by us. Store first, 200 fast, understand later.

## Auth

Send your token as either header:

```
Authorization: Bearer <token>
x-s2s-token: <token>
```

Two kinds of caller are accepted, and the door decides by what the token
claims — not by trying one and falling back:

| Caller | Token | How it's checked |
| ------ | ----- | ---------------- |
| **Relay** — one service pushing for many merchants (e.g. the Shopify relay) | A wildcard-scoped RBAC JWT (`merchant_ids: ["*"]`, or an admin token). The same credential the lead API already takes. | Signature + expiry only. No per-merchant provisioning, no DB lookup. |
| **Single merchant** — one integration, one tenant (e.g. the WooCommerce plugin) | The per-merchant JWT stored in `merchants.s2s_token`. | Compared constant-time against the stored value, then verified. Rotating the row revokes the old token on the next call. |

Both token kinds are JWTs signed by the same key, so a valid signature
alone proves nothing about which kind you hold. Only a wildcard scope
does — the per-merchant mint always issues exactly one `merchant_id` —
which is why a narrowly-scoped token must always clear the stored value.

Failures: no token, bad signature, expired, or not the merchant's
current stored token → **401**. Narrowly-scoped token for a merchant
with nothing provisioned → **404**.

## Request

```bash
curl -X POST https://<host>/ingest/events \
  -H "Content-Type: application/json" \
  -H "x-s2s-token: $S2S_TOKEN" \
  -d '{
    "merchant_id": "m_123",
    "source":      "loyalty-svc",
    "topic":       "order.placed",
    "external_id": "order.placed:chk-88412",
    "payload": {
      "customer_mobile_number": "+919876543210",
      "customer_name":          "Priya Sharma",
      "order_id":               "42",
      "amount":                 99900
    },
    "occurred_at": "2026-08-25T10:00:00Z"
  }'
```

| Field            | Required | Notes                                                        |
| ---------------- | -------- | ------------------------------------------------------------ |
| `merchant_id`    | yes      | Tenant scope; must be within the token's scope               |
| `source`         | yes      | Your system's name — stable, lowercase (`loyalty-svc`)       |
| `topic`          | yes      | What happened (`order.placed`); routes the consumer belt     |
| `external_id`    | yes      | YOUR id for this event — the dedupe key. **Topic-qualify it**, see below |
| `payload`        | yes      | Any JSON object, stored verbatim and immutable. Standard keys below |
| `occurred_at`    | no       | When it happened at the source. **ISO-8601 with an offset** (`2026-08-25T10:00:00Z` or `+05:30`) — a naive timestamp is a 422, because we will not guess your zone. Future values are clamped to `now()`; **omitted also becomes `now()`** (receive time), so downstream timers measure from arrival |
| `schema_version` | no       | Defaults to `"1"`; bump when your payload shape changes      |

Unknown fields are rejected with **422** — including `customer_id`,
which is deliberately not accepted because attribution is the consumer
belt's job (`resolve()`'s monopoly, ADR 0020). A typo'd field name is a
422 rather than a silent drop, so `occured_at` fails loudly instead of
quietly storing the receive time.

### Naming the real producer

`source` names **whose letter this is, not which pipe carried it**. The
Shopify relay therefore sends `source: "shopify"` — not the name of the
service doing the relaying — and derives `external_id` from ids Shopify
itself issues (the order or checkout id), so either side can re-derive it
for a replay or a parity join without reaching into the pipe's own
database. The extractor registry keys on **`source`** (design/ingest-doors:
several topics from one source dispatch inside that source's extractor),
so the name chosen here decides which extractor reads the payload.

### `external_id` — unique across ALL topics

The dedupe key is `(merchant_id, source, external_id)`. **`topic` is not
part of it.** If your natural id repeats across topics — one checkout id
shared by the checkout, the order, and the cancellation — a bare id
silently collides:

```
POST external_id="chk-88412"  topic="checkout.initiated"  → 200 {"duplicate": false}
POST external_id="chk-88412"  topic="order.placed"        → 200 {"duplicate": true}   ← EATEN
```

The second event is dropped and the response still looks like success.
Prefix the topic to keep them distinct:

```
"external_id": "checkout.initiated:chk-88412"
"external_id": "order.placed:chk-88412"
```

If your source genuinely has no natural id, compose one — don't skip the
field.

### Standard payload keys

One generic extractor reads every payload; there are no per-merchant
decoders. It needs a handle to attribute the event to a customer:

| Key | Required | Notes |
| --- | -------- | ----- |
| `customer_mobile_number` | yes | E.164, e.g. `+919876543210` |
| `customer_name`          | yes | Display name |

Without them the event stores fine and then **quarantines as `no_handle`**:
no attribution → no enrolment → no call, and nothing in the journey. It
fails silently and late, so treat these as required even though the
envelope won't 422 on them.

Every other **scalar** key (≤256 chars) rides into the template's
`{placeholder}` variables — that's why fields like `item` and
`cart_value` belong in the payload.

## Responses

| Status | Body                              | Meaning                                                          |
| ------ | --------------------------------- | ---------------------------------------------------------------- |
| 200    | `{"id": "<uuid>", "duplicate": false}` | Stored — `id` is this event's receipt                       |
| 200    | `{"id": null, "duplicate": true}` | Already stored under `(merchant_id, source, external_id)` — success, not an error |
| 401    | —                                 | No token, or invalid/expired/superseded                          |
| 404    | —                                 | No per-merchant token provisioned for this merchant              |
| 413    | —                                 | Event larger than 1 MB — split it; one event per fact          |
| 422    | —                                 | Envelope invalid — blank or whitespace-only fields, non-object payload, or an unknown field |
| 503    | —                                 | Store failed — retry with the SAME `external_id` (dedupe makes it safe) |

## Producer rules of thumb

- Retry 503s (same `external_id`, idempotent); treat `duplicate: true`
  as success.
- One event per fact — don't batch several facts into one payload; the
  belt routes by `(source, topic)`.
- Never rewrite a sent event: rows are immutable by trigger. New
  information is a new event with a new `external_id`.
