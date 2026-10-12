"""messaging/logs.py: log lines stay bounded — a sweep names the rows it
touched, never more than a sample of them."""

from app.crm.connectivity.messaging.logs import sample_ids


def test_sample_ids_lists_everything_when_it_fits() -> None:
    """Sample ids lists everything when it fits."""
    assert sample_ids(["a", "b"], limit=10) == "a, b"
    assert sample_ids([], limit=10) == ""


def test_sample_ids_truncates_and_says_how_many_it_hid() -> None:
    """Sample ids truncates and says how many it hid."""
    # Batch size is operator-tunable and the sweeps are unbounded, so an
    # uncapped join could emit a multi-megabyte log record.
    out = sample_ids([f"id-{n}" for n in range(1000)], limit=3)
    assert out == "id-0, id-1, id-2 … +997 more"
    assert len(out) < 200
