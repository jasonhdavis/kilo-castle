"""Engine test fixtures.

kilo-castle is the framework source, not a Court-initialized workspace: it has
no `.court/` of its own (see the rip-out in the repo history). Role models,
model aliases, suite/harness commands and template lookups all resolve from the
*consumer* repo's `.court/` via ``court.config.find_court_dir`` — which walks up
from the current working directory.

Tests that exercise engine code paths that resolve from the CWD therefore need a
manifest to exist. This session fixture supplies a throwaway Court install built
from the packaged seed (``court/templates/config.template.json``), so the
engine's own repo stays clean while the tests stay hermetic.

It is deliberately fallback-only: if a test has chdir'd into a real repo that
carries its own ``.court/config.json``, the real one wins.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from court import config

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
_PKG = _ENGINE_ROOT / "court"


def _seed_court_dir(dest: Path) -> Path:
    """Write a minimal but faithful ``.court/`` (manifest + templates) into
    ``dest`` from the packaged seed, mirroring what ``court init`` installs."""
    court_dir = dest / ".court"
    templates = court_dir / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    (court_dir / "config.json").write_text(
        (_PKG / "templates" / "config.template.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    for tmpl in (_PKG / "templates").glob("*.md"):
        shutil.copy2(tmpl, templates / tmpl.name)
    return court_dir


@pytest.fixture(autouse=True, scope="session")
def engine_court_manifest(tmp_path_factory):
    court_dir = _seed_court_dir(tmp_path_factory.mktemp("engine-court"))
    original = config.find_court_dir

    def find_court_dir(start=None):
        real = original(start)
        return real if (real / "config.json").is_file() else court_dir

    config.find_court_dir = find_court_dir
    try:
        yield court_dir
    finally:
        config.find_court_dir = original
