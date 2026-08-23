"""assert_facts internals: the evidence ladder's winner logic and the
canon's inferred-confidence cap (0.5)."""

from app.crm.identity.facts import _winner, claim_confidence


def _claim(v: str, e: str, at: str, k: float = 1.0) -> dict:
    return {"v": v, "e": e, "k": k, "at": at}


def test_higher_evidence_wins() -> None:
    claims = [
        _claim("Ravi", "declared", "2026-08-01T00:00:00+00:00"),
        _claim("R. Kumar", "imported", "2026-08-20T00:00:00+00:00"),
    ]
    assert _winner(claims)["v"] == "Ravi"


def test_tie_breaks_to_newest() -> None:
    claims = [
        _claim("Ravi", "observed", "2026-08-01T00:00:00+00:00"),
        _claim("Ravi K", "observed", "2026-08-20T00:00:00+00:00"),
    ]
    assert _winner(claims)["v"] == "Ravi K"


def test_unknown_evidence_ranks_lowest() -> None:
    claims = [
        _claim("Ravi", "declared", "2026-08-01T00:00:00+00:00"),
        _claim("X", "someday-class", "2026-08-20T00:00:00+00:00"),
    ]
    assert _winner(claims)["v"] == "Ravi"


def test_inferred_confidence_capped() -> None:
    assert claim_confidence("inferred", None) <= 0.5
    assert claim_confidence("inferred", 0.9) == 0.5


def test_declared_defaults_full_confidence() -> None:
    assert claim_confidence("declared", None) == 1.0


def test_confidence_clamped_to_unit_interval() -> None:
    assert claim_confidence("observed", 1.7) == 1.0
    assert claim_confidence("observed", -0.2) == 0.0
