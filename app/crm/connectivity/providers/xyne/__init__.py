"""XyneSpaces — a merchant's Xyne app as a connector.

One account per Xyne app: ``external_account_id`` is the app id, the door's
``door_data`` holds the app's ``base_url``, and the credential bundle in the
vault holds its API token and the secret Xyne signs that app's webhooks with.
The faces answer to their composition roots (boundary rule 11): onboard.py and
actions.py to connectors.py, inbound.py to ingress.py. client.py is the HTTP transport and
never leaves this package.

Exports nothing: import each face by its full path (the re-export-hub scar).
"""
