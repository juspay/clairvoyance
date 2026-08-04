"""Conftest for alerts tests -- sets required env vars for imports."""

import os

# Must be set before the app's module-level imports fire.
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "60")
os.environ.setdefault("BREEZE_BUDDY_SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("BREEZE_BUDDY_DASHBOARD_USERNAME", "test_admin")
os.environ.setdefault("BREEZE_BUDDY_DASHBOARD_PASSWORD", "test_password")
os.environ.setdefault("BB_DISPATCH_QPS_JITTER_MS", "100")
