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
