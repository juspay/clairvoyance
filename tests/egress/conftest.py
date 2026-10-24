import pytest

import app.core.security.ssrf as ssrf


@pytest.fixture(autouse=True)
def _fresh_egress_caches():
    """Checked DNS answers and TLS contexts are cached per process."""
    ssrf._DNS_CACHE.clear()
    ssrf._TLS_CONTEXTS.clear()
    yield
    ssrf._DNS_CACHE.clear()
    ssrf._TLS_CONTEXTS.clear()
