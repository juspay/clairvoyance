# Xyne apps — connect, receive form submissions, post messages

A merchant's **Xyne app** is a connector account: `xyne:<appId>`. Once
connected, the app's form submissions arrive as `flow_action` events on the
spine, and a workflow can call the app's Xyne API (e.g. post to a channel)
with `{connector: "xyne", action: "request"}`, configuring the `url` itself.

## 1. Connect an app

`POST /connectors/xyne/onboard` (merchant-scoped, like every connector):

```json
{
  "merchant_id": "flow-shop",
  "app_id": "app-123",
  "api_token": "<the app's Xyne API token>",
  "webhook_secret": "<the secret Xyne signs this app's webhooks with>",
  "base_url": "https://spaces.xyne.juspay.net",
  "display_label": "Hiring app",
  "door_name": "hiring-app"
}
```

- `base_url` is required (there is no default) and must be `https://` on a
  `*.xyne.juspay.net` host — the token is never sent anywhere else.
- `door_name` is optional: a plain token (letters, digits, `.`, `_`, `-`;
  max 64), unique per merchant on the connector. A workflow action can name
  it to act through THIS app; a name already used by another of the
  merchant's Xyne apps is refused. Re-onboarding without one keeps the
  existing name.
- Before anything is written, onboarding calls `GET {base_url}/api/apps` with
  `Authorization: Bearer <api_token>`. A 401/403 refuses the connection; an
  unreachable Xyne refuses it too (try again).
- On success: one row in `credentials` (custom, named
  `xyne:<merchant>:<appId>`) holding `api_token` and `webhook_secret`; one
  `crm_connector_installation` row with `connector_key = 'xyne'`,
  `external_account_id = <appId>`, status `healthy`, and
  `door_data = {"base_url": "https://spaces.xyne.juspay.net"}` (migration
  085 — where the app lives is not a secret, so it sits on the door).
  Re-onboarding the same app rotates the credential and door_data in place.

## 2. Point the app's webhook at us

In Xyne, set the app's webhook URL to:

```
https://<our host>/ingest/webhooks/xyne/<appId>
```

Each app has its own URL. The app id in the path is how the door finds THAT
app's secret before trusting the body:

| Check | Answer |
|---|---|
| app id held by no usable door (unknown, revoked, disabled) | 403 |
| credential missing / unreadable, or no `webhook_secret` | 403 |
| DB or vault error while looking up the app or its secret | 200 + error reply, nothing filed |
| `x-xyne-signature` is not HMAC-SHA256 (hex, optional `sha256=`) of the raw body with that app's secret | 403 |
| signed, and any type other than `submit` (e.g. `inputChange`) | 200 + `{"type": "ack"}`, nothing filed |
| signed `submit` missing `actionId`, `values` or `context.messageId` | 200 + error reply, nothing filed |
| store failed | 200 + error reply (Xyne never retries; the person resubmits) |
| filed | 200 + `{"type": "close_screen", "finalMessage": "Thanks, your response is saved"}` |

The merchant is **the app's owner** — never a `merchant_id` in the body.
The event's `external_id` is `<context.messageId>:<hash of actionId + values>`:
the same submission replayed is deduped, a changed answer on the same form is
stored as a new event.

## 3. Use it in a workflow

The catalog entry is **Form submitted** (`source: xyne`, topic
`flow_action`, about a customer). The person is resolved from
`context.flowJSON.data.customer_mobile_number`, so the form a workflow sends
must carry it. Fields include `actionId`, `values.selected_dates`,
`messageId`, `conversationId`, `userId`, `kind`, and the derived
`{selected_slots}` (`2026-10-15: 10:00, 11:00`, one line per date).

`request` args — the workflow configures the call; the door supplies the
host (`door_data.base_url`) and the vault the token:

| Arg | |
|---|---|
| `url` | required — a path under the base URL, e.g. `/api/apps/chat/postMessage`; never a scheme or host |
| `method` | `POST` (default), `GET`, `PUT` or `PATCH` |
| `body` | JSON body; `{placeholders}` anywhere inside it are resolved |

The action node may also say `"door_name": "<name>"` (optional). Given, the
action acts through the app onboarded under that name, and the run **parks**
if no usable app has it — it never falls back to another app. Absent, it acts
through the merchant's newest usable Xyne app.

Posting a message (with an optional FlowJSON form):

```json
{ "type": "action", "connector": "xyne", "action": "request",
  "args": { "url": "/api/apps/chat/postMessage", "method": "POST",
            "body": { "channelId": "<channel>", "text": "Pick your slots",
                      "flow": { "data": { "enrollment_id": "{run_id}" } } } } }
```

An action square may also ask for `{run_id}` and `{customer_mobile_number}`
(filled from the run itself, action args only). Put `{run_id}` in the form's
`data.enrollment_id` and the wait can hear only its own run's answer:

```json
{ "type": "wait", "topics": ["flow_action"], "key": "actionId",
  "match": { "payload": "context.flowJSON.data.enrollment_id", "run": "id" } }
```

Worked examples: `docs/crm/plans/xyne-availability-resume.json` (send the
form, wait, post the picks) and `xyne-availability-start.json` (enter on a
submission).

Failures: a 4xx from Xyne or an unusable credential **parks** the run; a 5xx,
429 or timeout is retried by the walker.

## Open items

- **Confirm against live Xyne before going live:** the signature header and
  scheme, and the `/api/apps/chat/postMessage` body (`channelId`, `text`,
  `flow`) the example plans send.
- **Callbacks other than submit** (an `inputChange` on every field edit, or
  any other `type`) are answered `{"type": "ack"}` and not filed. A body that
  says `submit` but is off-shape gets the error reply so the person can
  resubmit. (`MESSAGE_POSTED` is not a callback: it is `postMessage`'s own
  synchronous 201 answer.)
- **Several apps per merchant:** an action without `door_name` acts through
  the merchant's newest usable Xyne app; give each app a `door_name` and name
  it on the node to pick one. Receiving works for any number of apps.
- **One merchant per app id:** an app id is one merchant's. Onboarding
  refuses an API token whose `appId` claim is not the app being connected,
  and answers 409 when another merchant already holds the app (disconnect it
  there first). A callback is filed under the merchant whose door verified
  its signature.
