"""XyneSpaces — a merchant's Xyne app as a connector.

One account per Xyne app: ``external_account_id`` is the app id, and the
credential bundle in the vault (encrypted with CREDENTIAL_ENCRYPTION_KEY)
holds the app's API token, the secret Xyne signs that app's webhooks with,
and the base URL its API lives at. The faces answer to their composition
roots (boundary rule 11): onboard.py to connectors.py, inbound.py to
ingress.py. client.py is the HTTP transport and never leaves this package.

Exports nothing: import each face by its full path (the re-export-hub scar).
"""
