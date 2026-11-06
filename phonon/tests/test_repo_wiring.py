import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_PYPROJECT = REPO_ROOT / "pyproject.toml"


def _in_clairvoyance_repo() -> bool:
    if not ROOT_PYPROJECT.is_file():
        return False
    project = tomllib.loads(ROOT_PYPROJECT.read_text()).get("project", {})
    return project.get("name") == "clairvoyance"


pytestmark = pytest.mark.skipif(
    not _in_clairvoyance_repo(), reason="phonon copied outside the clairvoyance repo"
)


def test_clairvoyance_image_excludes_phonon():
    # The root Dockerfile does `COPY . .`; without this line the STT service
    # (and later its GPU dependencies) would ship inside every Clairvoyance pod.
    lines = (REPO_ROOT / ".dockerignore").read_text().splitlines()
    assert "phonon/" in lines


def test_clairvoyance_typecheck_excludes_phonon():
    config = tomllib.loads(ROOT_PYPROJECT.read_text())
    assert "phonon" in config["tool"]["pyrefly"]["project-excludes"]
