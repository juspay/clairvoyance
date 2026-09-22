"""The ceiling on one log message, and the flag that admits to it.

Vector DISCARDS a line over max_line_bytes rather than trimming it, so
before this cap an oversized line never reached the log store. Silent loss
is the failure mode here — hence is_truncated is asserted as hard as the
cut itself: a shortened line nobody can identify is the same bug one layer
down.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from app.core.config.static import LOG_MAX_MESSAGE_BYTES
from app.core.logger import (
    TRUNCATION_MARKER,
    json_sink,
    log_context_patcher,
    truncate_message,
)


def _emit(
    capsys: pytest.CaptureFixture, message: str, level: str = "INFO", **bound: Any
) -> Dict[str, Any]:
    """One line through the REAL patcher and json_sink.

    Not logger.info(): sinks run with enqueue=True, so delivery is on a
    background thread and captured stdout would race it.
    """
    record: Dict[str, Any] = {
        "time": datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
        "level": SimpleNamespace(name=level),
        "name": "app.database.accessor.breeze_buddy.lead_call_tracker",
        "function": "get_lead_by_id",
        "line": 364,
        "message": message,
        "module": "lead_call_tracker",
        "process": SimpleNamespace(id=1),
        "thread": SimpleNamespace(id=1),
        "extra": dict(bound),
    }
    log_context_patcher(record)
    json_sink(SimpleNamespace(record=record))
    return json.loads(capsys.readouterr().out)


def _emit_raw(capsys: pytest.CaptureFixture, message: str, **bound: Any) -> int:
    """Bytes the sink actually wrote — what the collector measures."""
    record: Dict[str, Any] = {
        "time": datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
        "level": SimpleNamespace(name="INFO"),
        "name": "app.database.accessor.breeze_buddy.lead_call_tracker",
        "function": "get_lead_by_id",
        "line": 364,
        "message": message,
        "module": "lead_call_tracker",
        "process": SimpleNamespace(id=1),
        "thread": SimpleNamespace(id=1),
        "extra": dict(bound),
    }
    log_context_patcher(record)
    json_sink(SimpleNamespace(record=record))
    return len(capsys.readouterr().out.strip().encode("utf-8"))


# --- the pure helper ---


def test_a_message_under_the_limit_is_untouched() -> None:
    """The common case must cost nothing and change nothing."""
    text, original = truncate_message("Lead found: abc", limit=8192)
    assert text == "Lead found: abc"
    assert original is None


def test_an_oversized_message_is_cut_and_reports_its_old_size() -> None:
    text, original = truncate_message("x" * 100, limit=10)
    assert text == "x" * 10 + TRUNCATION_MARKER
    assert original == 100


def test_the_limit_is_bytes_not_characters() -> None:
    """Measuring characters would let a multibyte line pass here and
    still be discarded at the collector."""
    text, original = truncate_message("🙂" * 10, limit=10)  # 4 bytes each
    assert original == 40
    assert len(text.encode("utf-8")) <= 10 + len(TRUNCATION_MARKER.encode("utf-8"))


def test_a_multibyte_character_is_never_split_in_half() -> None:
    """The cut lands mid-character (limit 10, 4-byte chars). One invalid
    byte makes the whole JSON line unparseable — worse than a long line."""
    text, _ = truncate_message("🙂" * 10, limit=10)
    assert text.encode("utf-8").decode("utf-8") == text  # round-trips
    assert text.startswith("🙂🙂")  # two whole chars, the third dropped


def test_an_exactly_sized_message_is_not_cut() -> None:
    """Off-by-one guard: the limit is inclusive."""
    text, original = truncate_message("x" * 10, limit=10)
    assert text == "x" * 10 and original is None


# --- through the real logger and sink ---


def test_a_giant_line_reaches_the_sink_shortened_and_flagged(
    capsys: pytest.CaptureFixture,
) -> None:
    """What the change is for: the line SURVIVES. Before the cap the
    collector dropped it and no rule or grep could ever see it."""
    giant = "Lead found: " + "X" * 60000
    line = _emit(capsys, giant, lead_id="abc")

    assert line["is_truncated"] is True
    assert line["original_bytes"] == len(giant)
    assert line["message"].endswith(TRUNCATION_MARKER)
    # Against the CAP, not a magic number — this cannot rot when the cap moves.
    assert len(line["message"].encode("utf-8")) <= LOG_MAX_MESSAGE_BYTES + len(
        TRUNCATION_MARKER.encode("utf-8")
    )


def test_truncation_keeps_the_fields_and_the_level(
    capsys: pytest.CaptureFixture,
) -> None:
    """Only the tail of the TEXT is lost — bound fields are what rules
    read, so losing them would defeat the point of shipping the line."""
    line = _emit(
        capsys, "y" * 60000, level="ERROR", lead_id="abc", reseller_id="breeze"
    )

    assert line["lead_id"] == "abc" and line["reseller_id"] == "breeze"
    assert line["level"] == "ERROR"


def test_a_normal_line_carries_neither_flag(capsys: pytest.CaptureFixture) -> None:
    """Absent, not false: `WHERE is_truncated = true` is then the whole
    query for finding emitters to fix."""
    line = _emit(capsys, "Lead found: small one", lead_id="def")

    assert "is_truncated" not in line
    assert "original_bytes" not in line
    assert line["message"] == "Lead found: small one"


def test_the_shipped_line_stays_under_the_collectors_limit(
    capsys: pytest.CaptureFixture,
) -> None:
    """The contract is BYTES ON THE WIRE, not bytes measured.

    The envelope and escaping sit between the cap and what Vector counts:
    with ensure_ascii=True a Kannada message capped at 16 KB shipped ~34 KB
    and was discarded anyway, so the cap bought nothing.
    """
    line_bytes = _emit_raw(capsys, "ಮಾಡಿ ಸಾರಥಿ " * 20000, lead_id="abc")
    assert line_bytes < 32768, f"shipped {line_bytes} B — Vector would discard it"


def test_non_ascii_is_not_escape_expanded(capsys: pytest.CaptureFixture) -> None:
    """Kannada must cost its UTF-8 size, not 2x it — that is what makes
    LOG_MAX_MESSAGE_BYTES mean what it says. Sized under the cap so nothing
    truncates, and large enough that the envelope is noise.
    """
    text = "ಮಾಡಿ" * 1000  # 12,000 UTF-8 bytes, under the cap
    utf8 = len(text.encode("utf-8"))

    shipped = _emit_raw(capsys, text)
    escaped = len(json.dumps(text).encode("utf-8"))  # what the default would cost

    assert shipped < utf8 * 1.05, "non-ASCII is being escape-expanded"
    assert escaped > utf8 * 1.9, "sanity: the default really does double it"


# --- the dial itself ---


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, 24576),  # unset
        ("8192", 8192),  # honoured
        ("131072", 131072),  # an operator who raised the collector's limit too
        ("abc", 24576),  # garbage must not stop a pod booting
        ("", 24576),
        ("0", 24576),  # would make every line the marker alone
        ("-1", 24576),
    ],
)
def test_the_cap_dial_survives_a_bad_value(
    monkeypatch: pytest.MonkeyPatch, raw: Any, expected: int
) -> None:
    """Garbage falls back rather than raising, like every knob in
    static.py — the raw int() this replaced crashed the pod at import.
    No upper bound: the collector's limit is the operator's to set."""
    from app.core.config.static import _positive_int

    monkeypatch.delenv("LOG_MAX_MESSAGE_BYTES", raising=False)
    if raw is not None:
        monkeypatch.setenv("LOG_MAX_MESSAGE_BYTES", raw)

    assert _positive_int("LOG_MAX_MESSAGE_BYTES", 24576) == expected
