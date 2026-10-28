# Xyne apps — connect an app

A merchant's **Xyne app** is a connector account, `xyne:<appId>`. A merchant
can connect any number of apps; each app belongs to one merchant.

## 1. In Xyne (once per app)

Done with a Xyne login, not the app token (Xyne Apps guide, "Setup"):

1. `POST /api/apps/create` (needs XYNE-APPS **Write**) → the app id, e.g. `app_hire01`.
2. `POST /api/apps/install/:appId` (needs **Admin**) → the app's API token,
   shown **once**. Keep it for step 2 below.
3. Get the app's webhook signing secret (ask the Xyne team where it is shown).

## 2. Connect it to Buddy

`POST /connectors/xyne/onboard` (merchant-scoped, like every connector):

```json
{
  "merchant_id": "pragathi",
  "api_token": "<the app's Xyne API token>",
  "webhook_secret": "<the secret Xyne signs this app's webhooks with>",
  "base_url": "https://spaces.xyne.juspay.net",
  "display_label": "Hiring bot"
}
```

There is no `app_id` field: the app id is read from the token's `appId` claim.

What Buddy checks, in order:

| Check | If it fails |
|---|---|
| Caller is logged in and may act for `merchant_id` | 401 / 403 |
| `base_url` is `https://` on a `*.xyne.juspay.net` host, with no path, query, fragment or login | 422 |
| `webhook_secret` is at least 16 characters | 422 |
| The token carries an `appId` | 400, Xyne never called |
| Xyne accepts the token: `GET {base_url}/api/apps/channel/list?limit=1&joined=true` with `Authorization: Bearer <token>`. 200, or 403 `missing_permission` (the token is genuine, the app only lacks `channels:read`), passes | 400 (401, another 403, or another 4xx), or 400 "try again" (Xyne unreachable, 5xx, 429, redirect) |
| No other merchant has this app connected (checked after the token is proven, and again under a lock in the transaction) | 409 |
| The connection is not disabled by an administrator | 400 |

On success:

- **`credentials`**: one row named `xyne:<merchant>:<appId>`, encrypted
  with AES-256-GCM under `CREDENTIAL_ENCRYPTION_KEY`: `api_token`,
  `webhook_secret`, `base_url`.
- **`crm_connector_installation`**: `connector_key = 'xyne'`,
  `external_account_id = <appId>`, `credential_id` → that row, status
  `healthy`. The response is this row; it never contains a secret.

Connecting the same app again replaces the token, secret and base URL in place.
Disconnecting sets the row to `revoked`; after that another merchant may
connect the app.

## 3. Before the first prod connection

Run on the replica (read-only):

```sql
SELECT is_encrypted, count(*) FROM credentials GROUP BY is_encrypted;
```

Recent `false` rows mean `CREDENTIAL_ENCRYPTION_KEY` is missing or invalid
(not 32 bytes): the vault then stores credentials as plain JSON, with only a
log line. Fix the key before connecting apps.

## 4. Receive form answers

Set the app's webhook URL in Xyne — once per app, in each workspace the app
is installed in — with a Xyne login that has XYNE-APPS **Write**
(`POST /api/apps/configureWebhook/:appId`):

```json
{ "webhookUrl": "https://<buddy host>/ingest/webhooks/xyne/<appId>" }
```

Every callback of that app arrives there, signed with the app's signing
secret (`X-Xyne-Signature`, plain-hex HMAC-SHA256 of the body). Buddy finds
the app's connection from `<appId>` in the URL — the merchant is the app's
owner, never anything in the body — and checks the signature with that
app's secret.

| Callback | Buddy answers | Stored |
|---|---|---|
| `X-Xyne-Event: flow_action`, `type: "submit"` | 200 `{"type":"close_screen","finalMessage":"Response received"}` | one `xyne` / `flow_action` event; one person's same answer twice is stored once |
| A field edit (`type: "inputChange"`) or any other app event | 200 `{"type":"ack"}` | nothing |
| Wrong signature, unknown app, disconnected app | 403 | nothing |
| Body over 1 MB / not JSON | 413 / 400 | nothing |
| A read or the store failed | 503 `{"type":"error","message":"Couldn't save your response, please submit again","retryable":true}` — Xyne shows it to the person | nothing |

What a workflow can use from a stored answer (catalog entry `xyne` /
`flow_action`, "Form submitted"):

| Field | Is | Use |
|---|---|---|
| `context.userId` | who answered — identity `xyne_user_id` | finds the customer; Xyne's server sets it, the person cannot |
| `reply` | `form_submitted` | the arrow out of a listening square |
| `button` | the `actionId` of the button pressed | branch on which button. The person's browser sends it and Xyne does not check it against the form, so it is the person's claim, like their answers |
| `flow_response` | every answer as `name: value` lines | one blank in a call or message (≤ 1000 characters; longer is dropped and a step that needs it parks) |
| `replied_to` | `context.messageId` | which of our sends it answers — finds the run |

`context.flowJSON` (the form as the person's browser sent it back) is stored
with the event and never read: the person can edit it.

For the answer to find a person, the letter that starts their run must carry
`"xyne_user_id": "<their Xyne user id>"` at the top level, next to their
phone. Example plan: `docs/crm/plans/xyne-interview-availability.json`.

Known limits:

- **Which run an answer wakes.** An answer names the message the form was
  posted in (`replied_to`). Once Buddy sends the form itself (Xyne 4/5), the
  send records that message, and the answer wakes only the run that sent
  it. Until then — a form posted outside Buddy — the answer wakes EVERY run
  of that person that is listening for `flow_action`: a person with two such
  runs open has both woken by one answer.
- **Xyne's top-level fields become run facts.** As for every source, the
  answer's top-level scalars (`actionId`, `type`) are kept as facts of the
  square that heard it, and the answer becomes the run's latest letter: a
  later step reading `type` reads Xyne's `"submit"`, not the starting
  letter's `type`. Name the merchant's own facts so they do not collide.

