"""merchant_http — a merchant's OWN endpoint as a connector (enh A/03).

A square carries no URL, no credential and no transport (modules/05, the
`action` word). So the endpoint is a CONNECTOR: the merchant onboards a
named account — its base URL and one optional auth header — ONCE, in the
console, and a plan names `{connector: "merchant_http", action: "request",
args}` exactly as it names Shopify's `add_tag`, picking an account with
`door_name` when the merchant has several. Change the URL or rotate the
secret and no published plan moves. (The args shape — path, method, body,
facts — is this PR's proposal; the corpus has no merchant_http ruling yet.)

Two faces, one package, boundary rule 11 as everywhere else: `onboard.py`
(the door and its request model) and `actions.py` (the one verb,
`request`). The outbound call goes through the shared egress guard
(app/core/security/ssrf.py) on every hop and follows redirects only to the
same host — a merchant-configured URL is still a URL someone outside typed,
and without the guard the walker is a door from a console form into our
private network.

Where things live: the account name is the door's external_account_id and
door_name; the base URL is `door_data.base_url` (migration 085); the auth
header name and value live in the `credentials` table (migration 014),
written by the shared onboarding sync and read back at fire time by
`bundle_for`. `credential_id` is only a pointer to the vault row.
"""
