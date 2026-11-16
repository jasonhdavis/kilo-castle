"""Pre-pack mergeability probe + GATE conflict remediation (Q1569 follow-up).

`court collect` merges each candidate onto `castle` at its own pre-pack
convergence gate and drops the ones that conflict; because `collect_min_batch`
is a planner-only policy it never backfills, so a cohort planned without
checking convergence packed SHORT (5 planned, 3 conflicted, 2 packed). These
tests pin the fix: probe a GATE branch's mergeability before planning, hold a
non-converging branch out of the cohort, attempt one mechanical `rebase`
remediation, and escalate a persistent GATE conflict to a Steward.
"""
import subprocess
from pathlib import Path

from court import git_ops
from court import nightwatch as nw


def _q(qid, status, worktree="/tmp/nw-probe-wt", worker="idle", **kw):
    base = {
        "id": qid,
        "kind": "quest",
        "status": status,
        "worktree": worktree,
        "worker": {"state": worker},
        "gatekeeper_session_id": "",
        "cogship_id": "",
        "pending_audience": False,
    }
    base.update(kw)
    return base


def _snap(*quests):
    return {"quests": list(quests)}


def _free(tmp_path, name="wt"):
    p = tmp_path / name
    p.mkdir(exist_ok=True)
    return str(p)


def _workers(count=0, dirs=None):
    return {"count": count, "rss_mb": 0.0, "by_role": {}, "workers": [], "dirs": dirs or {}}


def _policy(**kw):
    base = dict(max_workers=3, max_spawns_per_tick=2, max_spawns_per_hour=12)
    base.update(kw)
    return nw.Policy(**base)


# ---------------------------------------------------------------------------
# git_ops.probe_merge_clean — real git, no worktree/index mutation
# ---------------------------------------------------------------------------

def _init_git_repo(repo: Path, branch: str = "castle") -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True)


def test_probe_reports_clean_for_a_non_overlapping_branch(tmp_path):
    _init_git_repo(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-q", "-b", "quest"], check=True)
    (tmp_path / "quest.txt").write_text("q\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "quest"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-q", "castle"], check=True)
    (tmp_path / "castle.txt").write_text("c\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "castle"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-q", "quest"], check=True)

    res = git_ops.probe_merge_clean(tmp_path, base_branch="castle")
    assert res["ok"] is True
    assert res["clean"] is True
    assert res["conflicts"] == []


def test_probe_reports_the_conflicting_files_without_touching_the_worktree(tmp_path):
    _init_git_repo(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-q", "-b", "quest"], check=True)
    (tmp_path / "f.txt").write_text("a\nQQ\nc\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qam", "quest-edit"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-q", "castle"], check=True)
    (tmp_path / "f.txt").write_text("a\nZZ\nc\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qam", "castle-edit"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-q", "quest"], check=True)
    before = (tmp_path / "f.txt").read_text(encoding="utf-8")

    res = git_ops.probe_merge_clean(tmp_path, base_branch="castle")
    assert res["ok"] is True
    assert res["clean"] is False
    assert "f.txt" in res["conflicts"]
    # A probe never rewrites the worktree file or leaves the index mid-merge.
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == before
    status = subprocess.run(["git", "-C", str(tmp_path), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    assert status.strip() == ""


def test_probe_is_unknown_for_a_missing_worktree(tmp_path):
    res = git_ops.probe_merge_clean(tmp_path / "nope", base_branch="castle")
    assert res["ok"] is False
    assert res["clean"] is False


# ---------------------------------------------------------------------------
# plan: a non-converging GATE branch is held out of the cohort ...
# ---------------------------------------------------------------------------

def test_a_non_converging_gate_branch_is_held_and_remediated(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "GATE", wt))
    state = nw.load_state(tmp_path)
    probes = {"Q1": {"clean": False, "conflicts": ["apps/orders/models.py"]}}

    actions, notes = nw.plan(snap, state, _policy(collect_min_batch=1), _workers(),
                             now=1000.0, gate_merge_probes=probes)
    kinds = [(a.kind, a.quest_ids) for a in actions]
    assert ("collect", ["Q1"]) not in kinds, "a doomed convoy must not be planned"
    assert ("rebase", ["Q1"]) in kinds, "the branch should get one mechanical rebase"
    assert any(n.get("key") == "gate_not_mergeable" for n in notes)


def test_a_converging_gate_branch_is_collected(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "GATE", wt))
    state = nw.load_state(tmp_path)
    probes = {"Q1": {"clean": True, "conflicts": []}}

    actions, _ = nw.plan(snap, state, _policy(collect_min_batch=1), _workers(),
                         now=1000.0, gate_merge_probes=probes)
    assert [(a.kind, a.quest_ids) for a in actions] == [("collect", ["Q1"])]


def test_a_thinned_cohort_waits_instead_of_packing_short(tmp_path):
    """The original defect: 5 planned, 3 conflicted, 2 packed. Excluding the
    non-converging branches leaves the cohort below `collect_min_batch`, which
    must WAIT for a later tick rather than pack the survivors short."""
    w1 = _free(tmp_path, "w1")
    w2 = _free(tmp_path, "w2")
    snap = _snap(_q("Q1", "GATE", w1), _q("Q2", "GATE", w2))
    state = nw.load_state(tmp_path)
    probes = {"Q1": {"clean": True, "conflicts": []},
              "Q2": {"clean": False, "conflicts": ["apps/orders/models.py"]}}

    actions, notes = nw.plan(snap, state, _policy(collect_min_batch=2), _workers(),
                             now=1000.0, gate_merge_probes=probes)
    assert not [a for a in actions if a.kind == "collect"], (
        "one mergeable Quest must not be packed as a short convoy")
    assert any(n.get("key") == "collect_cohort:new" for n in notes)
    assert any(n.get("key") == "gate_not_mergeable" for n in notes)


def test_a_behind_conflicting_gate_branch_rebases_exactly_once(tmp_path):
    """The drift lane already rebases a behind branch; the remediation lane must
    not add a second rebase for the same Quest."""
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "GATE", wt, git_status={"behind": 7, "behind_code": 3}))
    state = nw.load_state(tmp_path)
    probes = {"Q1": {"clean": False, "conflicts": ["f.py"]}}

    actions, _ = nw.plan(snap, state, _policy(), _workers(),
                         now=1000.0, gate_merge_probes=probes)
    assert [a.kind for a in actions].count("rebase") == 1


# ---------------------------------------------------------------------------
# ... and a persistent conflict routes to a Steward (the remediation path).
# ---------------------------------------------------------------------------

def test_a_persistent_gate_conflict_escalates_to_a_steward(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "GATE", wt))
    state = nw.load_state(tmp_path)
    state["quests"]["Q1"] = {"merge_conflict": {"files": ["apps/orders/models.py"], "ts": 900.0}}
    probes = {"Q1": {"clean": False, "conflicts": ["apps/orders/models.py"]}}

    actions, notes = nw.plan(snap, state,
                             _policy(collect_min_batch=1, enabled=("goad", "coin", "collect",
                                                                   "dispatch", "rebase", "steward")),
                             _workers(), now=1000.0, gate_merge_probes=probes)
    kinds = [a.kind for a in actions]
    assert "collect" not in kinds
    assert "rebase" not in kinds, "a marked conflict is not re-rebased (no churn)"
    assert "steward" in kinds, "a GATE conflict must reach the Steward remediation path"
    assert any(n.get("key") == "gate_not_mergeable" for n in notes)


def test_a_recorded_gate_conflict_without_a_probe_is_also_held(tmp_path):
    """Belt and braces: with no fresh probe, a recorded marker still holds the
    branch out of the cohort and still escalates."""
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "GATE", wt))
    state = nw.load_state(tmp_path)
    state["quests"]["Q1"] = {"merge_conflict": {"files": ["x.py"], "ts": 900.0}}

    actions, notes = nw.plan(snap, state,
                             _policy(collect_min_batch=1, enabled=("goad", "coin", "collect",
                                                                   "dispatch", "rebase", "steward")),
                             _workers(), now=1000.0)
    kinds = [a.kind for a in actions]
    assert "collect" not in kinds
    assert "steward" in kinds
    assert any(n.get("key") == "gate_not_mergeable" for n in notes)


# ---------------------------------------------------------------------------
# tick: probes GATE branches before planning and clears a stale marker.
# ---------------------------------------------------------------------------

def test_tick_probes_gate_branches_and_clears_a_stale_marker(tmp_path, monkeypatch):
    from court import atelier as _atelier
    from court import convoy_guard
    from court import requests as _requests

    wt = tmp_path / "wt"
    wt.mkdir()
    snap = {"quests": [_q("Q1", "GATE", str(wt), git_status={"head_sha": "hsha"})]}
    state = nw.load_state(tmp_path)
    state["quests"]["Q1"] = {"merge_conflict": {"files": ["x.py"], "ts": 1.0}}

    monkeypatch.setattr(nw, "snapshot", lambda repo, timeout=None: snap)
    monkeypatch.setattr(nw, "live_kilo_workers", lambda repo: _workers())
    monkeypatch.setattr(nw, "save_state", lambda repo, state: None)
    monkeypatch.setattr(nw, "_journal", lambda repo, event: None)
    monkeypatch.setattr(nw, "stale_conflict_ids", lambda *a, **k: set())
    monkeypatch.setattr(nw, "_promoted_cogships", lambda repo, candidates=None: set())
    monkeypatch.setattr(nw, "_bucket_raze", lambda repo, ids: ([], []))
    monkeypatch.setattr(nw, "_charter_ready", lambda repo, qmap, now=None: [])
    monkeypatch.setattr(nw.vigils, "ready_vigils", lambda repo, qmap, now=None: ([], []))
    monkeypatch.setattr(convoy_guard, "ui_mandate_outstanding", lambda repo, qid: (False, ""))
    monkeypatch.setattr(_atelier, "members", lambda repo: [])
    monkeypatch.setattr(_atelier, "worktree_path", lambda repo: tmp_path / "no-atelier")
    monkeypatch.setattr(_requests, "load_open", lambda repo: [])
    monkeypatch.setattr(nw.git_ops, "_run",
                        lambda cmd, cwd, timeout=60: {"ok": True, "exit_code": 0,
                                                      "stdout": "bsha", "stderr": ""})
    monkeypatch.setattr(nw.git_ops, "probe_merge_clean",
                        lambda wt, base_branch="castle", timeout=60: {
                            "ok": True, "clean": True, "conflicts": [], "error": None})

    seen = {}

    def fake_plan(*a, **k):
        seen.update(k)
        return [], []

    monkeypatch.setattr(nw, "plan", fake_plan)

    nw.tick(tmp_path, _policy(pickup=False), state, dry_run=True, say=lambda *a, **k: None)

    assert seen.get("gate_merge_probes", {}).get("Q1", {}).get("clean") is True
    assert "merge_conflict" not in state["quests"]["Q1"], (
        "a clean probe must clear a stale conflict marker")
