"""Teardown: worktree removal and stale pytest-database reaping.

The two destructive paths must be provably safe, so these tests pin both the
name arithmetic (which must stay byte-identical to `core/settings/base.py`) and
every refusal: a dirty worktree, a path outside `.kilo/worktrees/`, a live
database hash, and a database with an attached backend.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from court import teardown


def _git(cwd: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "castle")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README").write_text("trunk\n", encoding="utf-8")
    _git(root, "add", "README")
    _git(root, "commit", "-qm", "trunk")
    (root / ".kilo" / "worktrees").mkdir(parents=True)
    return root


def _worktree(repo: Path, name: str, branch: str) -> Path:
    path = repo / ".kilo" / "worktrees" / name
    _git(repo, "worktree", "add", "-q", "-b", branch, str(path), "castle")
    return path


def test_db_suffix_matches_the_settings_formula():
    path = Path("/Users/scrummage/Python/pb-app/.kilo/worktrees/the-gatehouse-cogship-500")
    expected = hashlib.sha1(str(path).encode()).hexdigest()[:8]
    assert teardown.db_suffix(path) == expected


def test_db_name_parsing_accepts_worker_suffixes_and_rejects_lookalikes():
    prefixes = ("test_pb_app_local",)
    ok = teardown._split_db("test_pb_app_local_03e2c324_gw7", prefixes)
    assert ok and ok.hash == "03e2c324"
    assert teardown._split_db("test_pb_app_local_03e2c324", prefixes)
    # A mirror/dev database, a non-hex suffix, and a worker suffix we do not
    # emit must never be classified as a test database.
    assert teardown._split_db("pb_app_local", prefixes) is None
    assert teardown._split_db("test_pb_app_local_notahash", prefixes) is None
    assert teardown._split_db("test_pb_app_local_03e2c324_worker1", prefixes) is None
    assert teardown._split_db("test_pb_custom_03e2c324", prefixes) is None


def test_stale_selection_keeps_live_hashes_and_busy_databases(repo, monkeypatch):
    live_wt = _worktree(repo, "quest-q1", "quest/q1")
    live = teardown.db_suffix(live_wt)
    dbs = [
        teardown._Db(name=f"test_pb_app_local_{live}", size_bytes=100),
        teardown._Db(name="test_pb_app_local_deadbeef", size_bytes=200),
        teardown._Db(name="test_pb_app_local_cafebabe_gw1", size_bytes=300),
    ]
    monkeypatch.setattr(teardown, "_has_connections",
                        lambda name, cwd: name.endswith("cafebabe_gw1"))
    plan = teardown.plan_stale_test_dbs(repo, databases=dbs)
    assert [d.name for d in plan.kept_live] == [f"test_pb_app_local_{live}"]
    assert [d.name for d in plan.kept_busy] == ["test_pb_app_local_cafebabe_gw1"]
    assert [d.name for d in plan.stale] == ["test_pb_app_local_deadbeef"]
    assert plan.reclaimed_bytes == 200


def test_teardown_refuses_a_dirty_worktree(repo):
    wt = _worktree(repo, "quest-q1", "quest/q1")
    (wt / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")
    report = teardown.teardown_worktree(repo, wt, branch="quest/q1")
    assert not report["ok"]
    assert wt.exists()
    assert any("dirty" in step for step in report["steps"])


def test_teardown_refuses_a_path_outside_the_worktree_root(repo):
    outside = repo / "not-a-worktree"
    outside.mkdir()
    report = teardown.teardown_worktree(repo, outside)
    assert not report["ok"]
    assert outside.exists()
    assert any("not under" in step for step in report["steps"])


def test_teardown_keeps_an_unmerged_branch_but_removes_the_worktree(repo):
    """A squash-promoted Quest has its content in castle while the tip is
    unreachable. The worktree is disposable; deleting that ref would not be."""
    wt = _worktree(repo, "quest-q1", "quest/q1")
    (wt / "work.txt").write_text("feature\n", encoding="utf-8")
    _git(wt, "add", "work.txt")
    _git(wt, "commit", "-qm", "feature work")
    report = teardown.teardown_worktree(repo, wt, branch="quest/q1", drop_dbs=False)
    assert report["ok"], report["steps"]
    assert not wt.exists()
    assert any(step.startswith("kept branch") for step in report["steps"])
    branches = subprocess.run(["git", "branch", "--list", "quest/q1"],
                              cwd=str(repo), capture_output=True, text=True).stdout
    assert branches.strip()


def test_teardown_removes_a_clean_merged_worktree_and_branch(repo):
    wt = _worktree(repo, "quest-q1", "quest/q1")
    report = teardown.teardown_worktree(repo, wt, branch="quest/q1", drop_dbs=False)
    assert report["ok"], report["steps"]
    assert not wt.exists()
    branches = subprocess.run(["git", "branch", "--list", "quest/q1"],
                              cwd=str(repo), capture_output=True, text=True).stdout
    assert branches.strip() == ""


def test_dry_run_removes_nothing(repo):
    wt = _worktree(repo, "quest-q1", "quest/q1")
    report = teardown.teardown_worktree(repo, wt, branch="quest/q1", drop_dbs=False, apply=False)
    assert report["ok"]
    assert wt.exists()
    assert any(step.startswith("would remove") for step in report["steps"])
