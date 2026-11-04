"""Integration tests for the pleasance branch-surface-doc lane (court.pickup).

A pleasance batch may carry the root-level ``AGENTS.md`` / ``PLEASANCE.md``
surface docs so a GitHub-facing checkout is oriented, but those docs ride the
branch and are NEVER merged onto castle (which keeps its own internal
``AGENTS.md``). A batch that contains ONLY surface docs advances the applied
marker without creating a trunk commit.
"""
import subprocess
from pathlib import Path

import pytest

from court import pickup


def _git(repo, *argv):
    return subprocess.run(["git", "-C", str(repo), *argv], check=True,
                          capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _pickup_state_outside_repo(tmp_path, monkeypatch):
    """Keep pickup applied-state inside the tmp sandbox so tests are hermetic."""
    monkeypatch.setattr(pickup.nightwatch, "state_dir", lambda repo: tmp_path / "native-state")


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "castle")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test Author")
    (repo / ".court").mkdir(parents=True)
    (repo / ".court" / "LEDGER.md").write_text(
        "- **2000-01-01T00:00:00Z** — realm founded\n", encoding="utf-8")
    (repo / ".court" / ".write.lock").write_text("", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init castle")
    _git(repo, "branch", "main")
    bare = tmp_path / "origin.git"
    _git(repo, "init", "--bare", str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-u", "origin", "castle")
    _git(repo, "push", "origin", "main")
    return repo


def _please(repo, builder):
    _git(repo, "checkout", "-b", "pleasance", "castle")
    builder(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "pleasance paperwork")
    _git(repo, "push", "-u", "origin", "pleasance")
    _git(repo, "checkout", "castle")


def _charter(qid, title, status="PLANNED", branch="quest/q998-surface-lane"):
    return (
        "---\n"
        f"id: {qid}\n"
        f"title: {title}\n"
        "kind: quest\n"
        f"status: {status}\n"
        f"branch: {branch}\n"
        "---\n\n"
        f"# {qid} — {title}\n\n"
        "## The Kingdom Requires\n"
        "Do the thing.\n\n"
        "## Prerequisites\n"
    )


def _silent():
    return lambda *a, **k: None


def _surface_builder(repo):
    (repo / "AGENTS.md").write_text("# AGENTS (pleasance orientation)\n", encoding="utf-8")
    (repo / "PLEASANCE.md").write_text("# PLEASANCE lane contract\n", encoding="utf-8")


def test_dry_run_lists_surface_docs_without_accepting(tmp_path):
    repo = _repo(tmp_path)
    _please(repo, _surface_builder)

    res = pickup.run(repo, dry_run=True, say=_silent())

    assert res.get("rejected") is None
    assert res.get("picked") == []
    assert res.get("surface_docs") == ["AGENTS.md", "PLEASANCE.md"]


def test_surface_only_advances_marker_without_merging_onto_castle(tmp_path):
    repo = _repo(tmp_path)
    _please(repo, _surface_builder)

    res = pickup.run(repo, say=_silent())

    assert res.get("skipped") == "surface docs only"
    assert res.get("surface_docs") == ["AGENTS.md", "PLEASANCE.md"]
    # The docs ride the branch but never land on castle.
    assert not (repo / "AGENTS.md").exists()
    assert not (repo / "PLEASANCE.md").exists()
    # The applied marker advanced, so the next scraper diffs cleanly.
    again = pickup.run(repo, say=_silent())
    assert again.get("skipped") == "already applied"


def test_surface_doc_rides_alongside_a_real_charter(tmp_path):
    repo = _repo(tmp_path)

    def builder(repo):
        (repo / "AGENTS.md").write_text("# AGENTS (pleasance orientation)\n", encoding="utf-8")
        (repo / ".court" / "quests").mkdir(parents=True, exist_ok=True)
        (repo / ".court" / "quests" / "Q998-Surface-Lane.md").write_text(
            _charter("Q998-Surface-Lane", "Charter riding with a surface doc"),
            encoding="utf-8")

    _please(repo, builder)

    res = pickup.run(repo, say=_silent())

    assert res.get("rejected") is None
    assert res.get("picked") == [".court/quests/Q998-Surface-Lane.md"]
    # The charter is merged onto castle; the surface doc is not.
    assert (repo / ".court" / "quests" / "Q998-Surface-Lane.md").exists()
    assert not (repo / "AGENTS.md").exists()