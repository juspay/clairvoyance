"""The migration CI guard: duplicates, gaps, bad names."""

from pathlib import Path
from typing import List

import pytest

import scripts.check_migrations as cm


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: List[str]) -> int:
    for name in names:
        (tmp_path / name).write_text("SELECT 1;")
    monkeypatch.setattr(cm, "MIGRATIONS_DIR", tmp_path)
    return cm.main()


def test_clean_sequence_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(tmp_path, monkeypatch, ["001_a.sql", "002_b.sql", "003_c.sql"]) == 0


def test_duplicate_number_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _run(tmp_path, monkeypatch, ["001_a.sql", "001_b.sql"]) == 1


def test_gap_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(tmp_path, monkeypatch, ["001_a.sql", "003_c.sql"]) == 1


def test_bad_filename_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(tmp_path, monkeypatch, ["001_a.sql", "2_bad-name.sql"]) == 1
