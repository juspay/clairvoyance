"""Suppression liveness (expiry-as-predicate) — mirrors the 048 trigger."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.crm.platform.suppression import entry_is_live


def test_no_until_is_permanent() -> None:
    assert entry_is_live({"reason": "user_request"}) is True
    assert entry_is_live({"until": None}) is True


def test_future_until_is_live() -> None:
    until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert entry_is_live({"until": until}) is True


def test_past_until_has_lapsed() -> None:
    until = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert entry_is_live({"until": until}) is False


def test_garbage_until_fails_closed() -> None:
    assert entry_is_live({"until": "not-a-date"}) is True


def test_python_mirror_and_trigger_state_the_same_rule() -> None:
    # Tripwire until the DB-integration harness pins them live: if either
    # side's liveness rule changes, this fails and forces a look at the
    # other. entry_is_live is executable documentation; the trigger is
    # the authority.
    sql = Path("app/database/migrations/048_create_platform_identity.sql").read_text()
    assert "entry->>'until' IS NULL" in sql
    assert "(entry->>'until')::timestamptz > now()" in sql
    # the mirror's semantics, spot-pinned:
    assert entry_is_live({"until": None}) and not entry_is_live(
        {"until": "2000-01-01T00:00:00+00:00"}
    )
