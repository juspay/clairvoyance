"""The migration CI guard: duplicates, gaps, bad names — and the
base-union mode that makes cross-PR number collisions fail at PR time.

The union mode exists because CI checks out the PR HEAD: two open PRs
can each add the same migration number, each pass against its own tree,
and collide only after both merge (how the 052 duplicate landed on
release, 2026-08)."""

import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

import scripts.check_migrations as cm

REPO_ROOT = Path(cm.__file__).resolve().parent.parent


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: List[str]) -> int:
    for name in names:
        (tmp_path / name).write_text("SELECT 1;")
    monkeypatch.setattr(cm, "MIGRATIONS_DIR", tmp_path)
    return cm.main([])


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


def test_union_scenario_fires_the_052_escape() -> None:
    """The exact escape: the PR tree has 052_drop…, the base ALREADY has
    052_create… — the union (what CI now checks) must fail even though
    each side alone is clean."""
    pr_tree = ["051_x.sql", "052_drop_check.sql"]
    base = ["051_x.sql", "052_create_view.sql"]
    union = sorted(set(pr_tree) | set(base))
    errors = cm.collect_errors(union)
    assert any("duplicate migration number 052" in e for e in errors)
    # Each side alone passes — which is why HEAD-only checking missed it.
    assert cm.collect_errors(pr_tree) == []
    assert cm.collect_errors(base) == []


def test_sanctioned_renames_load_from_the_one_registry() -> None:
    """The union check excludes RENAMED_MIGRATIONS sources so the rename
    that FIXES a duplicate doesn't read as one. Loaded from migrate.py —
    a second copy would drift."""
    sources = cm.sanctioned_rename_sources()
    assert "052_create_crm_journey_view.sql" in sources
    assert "026_link_call_execution_config_to_template.sql" in sources


def test_print_sanctioned_lists_both_sides_of_every_rename() -> None:
    """The CI immutability guard derives its exemption filter from this
    output — it must carry OLD and NEW names for every registry entry
    (the 052→055 fix failed its own build when the guard's copy of the
    list was hardcoded separately)."""
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "check_migrations.py"),
            "--print-sanctioned",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    lines = set(result.stdout.split())
    assert "052_create_crm_journey_view.sql" in lines
    assert "055_create_crm_journey_view.sql" in lines
    assert "026_link_call_execution_config_to_template.sql" in lines
    assert "046_link_call_execution_config_to_template.sql" in lines


def test_script_passes_with_base_union_against_head() -> None:
    """Integration: `--base HEAD` unions the tree with itself (minus
    sanctioned renames) and must pass on a healthy checkout."""
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "check_migrations.py"),
            "--base",
            "HEAD",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
