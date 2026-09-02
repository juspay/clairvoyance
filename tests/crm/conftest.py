"""Shared test dials for tests/crm.

CRM_WEBHOOK_TEST_DSN lives here, not in app config: it is a test dial, and
the app's static config surface is for the app (review ruling, 2 Sep 2026).
Unset means the DB-backed integration tests skip.
"""

import os

CRM_WEBHOOK_TEST_DSN = os.environ.get("CRM_WEBHOOK_TEST_DSN") or None
