"""Platform connectors for the commerce flavor.

Each connector registers into the UCP layer's hooks (``ucp/hooks.py``) and
is otherwise invisible: the protocol modules never import a connector, so
adding a platform is adding a package here, never editing UCP code.
"""
