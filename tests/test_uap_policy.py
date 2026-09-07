"""Agentic policy from env: complete -> config; anything missing -> fail closed."""

import importlib


def _reload(monkeypatch, **env):
    for k in (
        "UAP_VERIFIED_NAMES",
        "UAP_MAX_PER_DRAW",
        "UAP_MAX_TOTAL",
        "UAP_MAX_DRAWS",
        "UAP_VALIDITY_DAYS",
        "UAP_LIMIT_CHOICES",
        "UAP_SELLER_NAME",
        "UAP_SELLER_MIC",
    ):
        monkeypatch.setenv(k, env.get(k, ""))
    import app.core.config.static as static
    import app.services.preview.uap.policy as policy

    importlib.reload(static)
    return importlib.reload(policy)


FULL = dict(
    UAP_VERIFIED_NAMES="Chennai Metro Rail Limited, Metropolitan Transport Corporation",
    UAP_MAX_PER_DRAW="200.00",
    UAP_MAX_TOTAL="1000.00",
    UAP_MAX_DRAWS="60",
    UAP_VALIDITY_DAYS="90",
    UAP_LIMIT_CHOICES="200.00,500.00,1000.00",
    UAP_SELLER_NAME="Chennai Metro Rail Limited",
    UAP_SELLER_MIC="CMRL",
)


def test_complete_env_builds_policy(monkeypatch) -> None:
    cfg, missing = _reload(monkeypatch, **FULL).load_agentic_policy()
    assert missing == [] and cfg is not None
    assert cfg.verified_names == [
        "Chennai Metro Rail Limited",
        "Metropolitan Transport Corporation",
    ]
    assert cfg.limits.max_draws == 60 and cfg.limit_choices == [
        "200.00",
        "500.00",
        "1000.00",
    ]
    assert cfg.seller_mic == "CMRL"


def test_missing_field_fails_closed(monkeypatch) -> None:
    env = dict(FULL)
    env.pop("UAP_SELLER_MIC")
    cfg, missing = _reload(monkeypatch, **env).load_agentic_policy()
    assert cfg is None and missing == ["UAP_SELLER_MIC"]


def test_malformed_amount_fails_closed(monkeypatch) -> None:
    cfg, missing = _reload(
        monkeypatch, **{**FULL, "UAP_MAX_PER_DRAW": "200"}
    ).load_agentic_policy()
    assert cfg is None and missing and missing[0].startswith("limits:")
