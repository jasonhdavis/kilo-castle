"""Deterministic blocker board tests (`court.blockers`).

Rows are exercised against real git worktrees so drift/conflict precedence is
honest; the Quest objects themselves are fabricated so the test does not need
a full event-log ledger.
"""
import json
import subprocess
import sys
from pathlib import Path

from court import blockers


def _git(repo, *argv):
    return subprocess.run(["git", "-C", str(repo), *argv], check=True,
                          capture_output=True, text=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "castle")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test Author")
    (repo / ".court" / "quests").mkdir(parents=True)
    (repo / ".court" / "LEDGER.md").write_text("- realm founded\n", encoding="utf-8")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init castle")
    return repo


class _Q:
    def __init__(self, qid, status, worktree="", app="pb-app", pending=False,
                 kind="quest", branch=""):
        self.id = qid
        self.status = status
        self.worktree = str(worktree) if worktree else ""
        self.app = app
        self.kind = kind
        self.branch = branch or "quest/" + qid.lower()
        self._pending = pending

    def has_pending_audience(self):
        return self._pending


def _fixture_board(tmp_path):
    repo = _repo(tmp_path)
    # two branches cut before castle advances => behind by one
    _git(repo, "branch", "quest/q020-conflict")
    _git(repo, "branch", "quest/q021-drift")
    (repo / "README.md").write_text("advanced\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "castle advances")
    _git(repo, "branch", "quest/q023-ready")

    wt_conflict = tmp_path / "wt-conflict"
    wt_drift = tmp_path / "wt-drift"
    wt_ready = tmp_path / "wt-ready"
    _git(repo, "worktree", "add", str(wt_conflict), "quest/q020-conflict")
    _git(repo, "worktree", "add", str(wt_drift), "quest/q021-drift")
    _git(repo, "worktree", "add", str(wt_ready), "quest/q023-ready")

    quests = [
        _Q("Q023-Ready", "WORKING", wt_ready),
        _Q("Q022-Decision", "HELD", "", pending=True),
        _Q("Q021-Drift", "TRIBUTE_READY", wt_drift),
        _Q("Q020-Conflict", "TRIBUTE_READY", wt_conflict),
    ]
    state = {"quests": {
        "Q020-Conflict": {"merge_conflict": {
            "files": ["court/cli.py", "court/a.py"], "ts": 1}},
    }}
    return repo, quests, state


def test_classifies_conflict_drift_decision_and_ready_worst_first(tmp_path):
    repo, quests, state = _fixture_board(tmp_path)
    payload = blockers.collect(repo, quests=quests, state=state)

    rows = {row["id"]: row for row in payload["quests"]}
    assert [row["id"] for row in payload["quests"]] == [
        "Q020-Conflict", "Q021-Drift", "Q022-Decision", "Q023-Ready"]

    conflict = rows["Q020-Conflict"]
    assert conflict["blocker"] == "conflict:court/a.py,court/cli.py"
    assert conflict["merge_conflict"] == ["court/a.py", "court/cli.py"]
    assert conflict["next"] == "court rebase Q020"
    assert conflict["behind"] == 1

    drift = rows["Q021-Drift"]
    assert drift["blocker"] == "drift:1"
    assert drift["behind"] == 1
    assert drift["ahead"] == 0
    assert drift["next"] == "court rebase Q021"

    decision = rows["Q022-Decision"]
    assert decision["blocker"] == "decision"
    assert decision["pending_audience"] is True
    assert decision["next"] == "await M'Lord ruling"
    assert decision["worktree"] == ""

    ready = rows["Q023-Ready"]
    assert ready["blocker"] == "ready"
    assert ready["next"] == "none"
    assert ready["behind"] == 0

    summary = payload["summary"]
    assert summary == {
        "total": 4, "parked": 2, "conflicts": 1, "drift": 1,
        "decisions": 1, "no_worktree": 0, "worker_stalled": 0, "ready": 1,
    }


def test_no_worktree_and_worker_stalled_classes(tmp_path):
    repo = _repo(tmp_path)
    quests = [
        _Q("Q030-NoWorktree", "WORKING", ""),
        _Q("Q031-ReadyGated", "GATE", "", branch="quest/q031-ready-gated"),
    ]
    payload = blockers.collect(repo, quests=quests, state={})
    rows = {row["id"]: row for row in payload["quests"]}
    assert rows["Q030-NoWorktree"]["blocker"] == "no-worktree"
    assert rows["Q030-NoWorktree"]["next"] == "dispatch Serf"
    assert rows["Q031-ReadyGated"]["blocker"] == "no-worktree"
    assert payload["summary"]["no_worktree"] == 2
    assert payload["summary"]["parked"] == 2


def test_terminal_and_epic_quests_are_not_rows(tmp_path):
    repo = _repo(tmp_path)
    quests = [
        _Q("Q040-Done", "DONE", ""),
        _Q("Q041-Epic", "WORKING", "", kind="epic"),
        _Q("Q042-Working", "WORKING", ""),
    ]
    payload = blockers.collect(repo, quests=quests, state={})
    assert [row["id"] for row in payload["quests"]] == ["Q042-Working"]


def test_summary_from_snapshot_matches_the_board_counts(tmp_path):
    snap = {"quests": [
        {"id": "Q1", "kind": "quest", "status": "TRIBUTE_READY",
         "worktree": "/x", "git_status": {"behind": 3}, "pending_audience": False},
        {"id": "Q2", "kind": "quest", "status": "GATE",
         "worktree": "/x", "git_status": {"behind": 0}, "pending_audience": False},
        {"id": "Q3", "kind": "quest", "status": "HELD",
         "worktree": "", "pending_audience": True},
        {"id": "Q4", "kind": "epic", "status": "WORKING", "worktree": "/x"},
        {"id": "Q5", "kind": "quest", "status": "DONE",
         "worktree": "", "git_status": {"behind": 5}},
    ]}
    state = {"quests": {"Q2": {"merge_conflict": {"files": ["a.py"]}}}}
    summary = blockers.summary_from_snapshot(snap, state)
    assert summary["total"] == 3
    assert summary["conflicts"] == 1
    assert summary["drift"] == 1
    assert summary["decisions"] == 1
    assert summary["parked"] == 2


def test_render_text_is_an_aligned_table(tmp_path):
    repo, quests, state = _fixture_board(tmp_path)
    text = blockers.render_text(blockers.collect(repo, quests=quests, state=state))
    lines = text.splitlines()
    assert lines[0].startswith("QUEST")
    assert "BLOCKER" in lines[0] and "NEXT" in lines[0]
    assert any("Q020" in line and "court rebase Q020" in line for line in lines)
    assert "4 non-terminal" in text
    assert "2 parked" in text


def test_cli_blockers_json_runs_from_the_worktree(tmp_path):
    repo, _quests, _state = _fixture_board(tmp_path)
    worktree_root = Path(__file__).resolve().parent.parent
    res = subprocess.run(
        [sys.executable, "-m", "court.cli", "blockers", "--repo", str(repo), "--json"],
        cwd=str(worktree_root), capture_output=True, text=True, timeout=120,
    )
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert "quests" in payload and "summary" in payload


def test_ready_to_raze_is_ready_despite_drift(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "branch", "quest/q050-merged")
    # castle races far ahead of a branch that was already promoted
    for i in range(3):
        (repo / "README.md").write_text(f"castle advance {i}\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", f"castle advance {i}")
    wt = tmp_path / "wt-merged"
    _git(repo, "worktree", "add", str(wt), "quest/q050-merged")

    quests = [_Q("Q050-Merged", "READY_TO_RAZE", wt)]
    payload = blockers.collect(repo, quests=quests, state={})
    row = payload["quests"][0]
    assert row["behind"] == 3
    assert row["blocker"] == "ready"
    assert row["next"] == "court raze Q050"
    assert payload["summary"]["drift"] == 0
    assert payload["summary"]["parked"] == 0
    assert payload["summary"]["ready"] == 1


def test_summary_from_snapshot_does_not_park_ready_to_raze():
    snap = {"quests": [
        {"id": "Q1", "kind": "quest", "status": "READY_TO_RAZE",
         "worktree": "/x", "git_status": {"behind": 591}, "pending_audience": False},
        {"id": "Q2", "kind": "quest", "status": "TRIBUTE_READY",
         "worktree": "/x", "git_status": {"behind": 4}, "pending_audience": False},
    ]}
    summary = blockers.summary_from_snapshot(snap, {})
    assert summary["drift"] == 1
    assert summary["parked"] == 1
    assert summary["ready"] == 1


def test_ready_to_raze_with_a_conflict_is_not_a_blocker(tmp_path):
    """A READY_TO_RAZE Quest with a stale merge_conflict marker must not surface
    the conflict (it is already promoted, not blocking a spawn)."""
    repo, _quests, _state = _fixture_board(tmp_path)
    quests = [_Q("Q060-Ready", "READY_TO_RAZE", "")]
    state = {"quests": {"Q060-Ready": {"merge_conflict": {"files": ["a.py"], "ts": 1}}}}
    payload = blockers.collect(repo, quests=quests, state=state)
    row = payload["quests"][0]
    assert row["blocker"] == "ready"
    assert row["merge_conflict"] == []
    assert payload["summary"]["conflicts"] == 0
    assert payload["summary"]["parked"] == 0


def test_summary_from_snapshot_ignores_a_terminal_conflict():
    snap = {"quests": [
        {"id": "Q1", "kind": "quest", "status": "READY_TO_RAZE",
         "worktree": "/x", "git_status": {}, "pending_audience": False},
    ]}
    state = {"quests": {"Q1": {"merge_conflict": {"files": ["stale.py"]}}}}
    summary = blockers.summary_from_snapshot(snap, state)
    assert summary["conflicts"] == 0
    assert summary["parked"] == 0
    assert summary["ready"] == 1
