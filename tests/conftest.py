"""Root test bootstrap: app env defaults.

app.core.config.static snapshots env vars at first app import (e.g.
``JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")``), so the frozen
value depends on which test module imports app code first. Per-file
``os.environ`` setup is therefore import-order-dependent: a full-suite
run can freeze "" before the file that sets the value is loaded, and
JWTManager then refuses to construct during collection. pytest imports
this conftest before any test module, so defaults set here always land
first. Real environment values (CI) win over setdefault.
"""

import os

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-not-used-by-these-tests")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
# No AWS in unit tests -- never attempt to reach KMS.
os.environ.setdefault("SKIP_KMS_DECRYPT", "true")
