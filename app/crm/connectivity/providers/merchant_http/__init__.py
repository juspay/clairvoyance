"""merchant_http — a merchant's OWN endpoint as a connector (enh A/03, the
ruled shape).

The corpus rules that a square carries no URL, no credential and no
transport (modules/05, the `action` word, 7 Sep 2026; A/03's `http` node
with an author URL is superseded). So the endpoint is a CONNECTOR: the
merchant onboards its base URL and one auth header ONCE, in the console,
and a plan names `{connector: "merchant_http", action: "request", args}`
exactly as it names Shopify's `add_tag`. Change the URL or rotate the
secret and no published plan moves.

Two faces, one package, boundary rule 11 as everywhere else: `onboard.py`
(the door and its request model) and `actions.py` (the one verb,
`request`). The outbound call goes through the shared egress guard
(app/core/security/ssrf.py) on every hop — a merchant-configured URL is
still a URL someone outside typed, and without the guard the walker is a
door from a console form into our private network.
"""
