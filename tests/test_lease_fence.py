"""Monotonic lease fence epoch + fenced suite proofs.

The fencing pattern (Mastra 1.73): each worker generation owns a lease epoch,
and engine-stamped suite proofs record the epoch that produced them. A proof
left behind by a worker that was killed/force-superseded must not be trusted
at promotion — redelivered or replacement workers fence off the old run.

Covered here:
- epoch starts at 1, steal/force bump it, self-reclaim does not
- legacy lease files without `epoch` read as 0 and never raise
- pending -> final `update()` preserves the epoch
- `fence_is_current` truth table (equal, stale+live, stale+no-holder, legacy)
- `_suite_lease_role` maps gatehouse worktrees to gatekeeper, quests to serf
- legacy proofs (no `lease_epoch`) still validate exactly as before
- a stale-fenced proof is rejected with both epochs named
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from court import git_ops, lease


# ---------------------------------------------------------------------------
# Helpers (same shapes as tests/test_worker_lease.py / test_convoy_guard.py)
# ---------------------------------------------------------------------------

def _sleeper(seconds=30):
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _seed_lease(worktree, role, payload):
    path = lease.lease_path(worktree, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _final_payload(pid, **extra):
    payload = {
        "v": 1, "pending": False, "pid": pid, "pgid": pid,
        "session_id": "ses_seed", "watchdog_pid": None, "holder": "dispatch",
        "model": "m", "started": time.time(), "heartbeat": time.time(),
    }
    payload.update(extra)
    return payload


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "castle")
    _git(repo, "config", "user.email", "fence@test")
    _git(repo, "config", "user.name", "fence")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return repo


def _head(repo):
    res = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return res.stdout.strip()


def _proof(repo, cogship_id="cogship-fence", **fields):
    directory = repo / ".court" / "suites"
    directory.mkdir(parents=True, exist_ok=True)
    proof = {
        "cogship_id": cogship_id,
        "worktree": str(repo),
        "command": "python3 -m pytest -q",
        "head_sha": _head(repo),
        "exit_code": 0,
        "ran_tests": 12,
        "finished": "2026-11-05T00:00:00Z",
    }
    proof.update(fields)
    (directory / f"{cogship_id}.json").write_text(json.dumps(proof), encoding="utf-8")
    return proof


# ---------------------------------------------------------------------------
# Epoch monotonicity
# ---------------------------------------------------------------------------

def test_a_fresh_claim_starts_at_epoch_one(tmp_path):
    ok, reason, _ = lease.claim(tmp_path, "serf", holder="dispatch", pid=424242)
    assert ok and reason == "claimed"
    assert lease.lease_epoch(tmp_path, "serf") == 1


def test_stealing_a_stale_lease_bumps_the_epoch(tmp_path):
    _seed_lease(tmp_path, "serf", _final_payload(999_999_999, epoch=1))
    ok, reason, _ = lease.claim(tmp_path, "serf", holder="goad", pending=True)
    assert ok and reason == "stolen-stale"
    assert lease.lease_epoch(tmp_path, "serf") == 2


def test_force_supersession_bumps_the_epoch(tmp_path):
    sleeper = _sleeper()
    try:
        _seed_lease(tmp_path, "serf", _final_payload(sleeper.pid, pgid=os.getpgid(sleeper.pid), epoch=4))
        ok, reason, _ = lease.claim(tmp_path, "serf", holder="goad", pending=True, force=True)
        assert ok and reason == "force-superseded"
        assert lease.lease_epoch(tmp_path, "serf") == 5
    finally:
        if lease._pid_alive(sleeper.pid):
            sleeper.kill()
            sleeper.wait(timeout=5)


def test_self_reclaim_in_the_same_process_does_not_bump_the_epoch(tmp_path):
    ok, reason, _ = lease.claim(
        tmp_path, "serf", holder="dispatch", pid=os.getpid(), pgid=os.getpgid(0),
        session_id="ses_self",
    )
    assert ok and reason == "claimed"
    assert lease.lease_epoch(tmp_path, "serf") == 1

    ok2, reason2, _ = lease.claim(
        tmp_path, "serf", holder="dispatch", pid=os.getpid(), pgid=os.getpgid(0),
        session_id="ses_self",
    )
    assert ok2, reason2
    assert lease.lease_epoch(tmp_path, "serf") == 1


def test_a_legacy_lease_without_epoch_reads_as_zero(tmp_path):
    _seed_lease(tmp_path, "serf", _final_payload(424242))  # no "epoch" key
    assert lease.lease_epoch(tmp_path, "serf") == 0
    fence = lease.holder_fence(tmp_path, "serf")
    assert fence["epoch"] == 0
    assert fence["pid"] == 424242
    assert lease.lease_epoch(tmp_path, "gatekeeper") == 0  # no file at all


def test_pending_to_final_update_preserves_the_epoch(tmp_path):
    ok, _, _ = lease.claim(tmp_path, "serf", holder="dispatch", pending=True)
    assert ok
    assert lease.lease_epoch(tmp_path, "serf") == 1

    updated = lease.update(tmp_path, "serf", pid=os.getpid(), pgid=os.getpgid(0), pending=False)
    assert updated is not None
    assert updated["epoch"] == 1
    assert lease.lease_epoch(tmp_path, "serf") == 1


def test_holder_fence_reports_an_absent_lease(tmp_path):
    fence = lease.holder_fence(tmp_path, "gatekeeper")
    assert fence == {
        "epoch": 0,
        "holder": "",
        "pid": None,
        "session_id": "",
        "live": False,
        "why": "no lease",
    }


# ---------------------------------------------------------------------------
# fence_is_current truth table
# ---------------------------------------------------------------------------

def test_fence_is_current_when_epochs_match():
    assert lease.fence_is_current({"epoch": 3}, 3) is True


def test_a_stale_epoch_with_a_live_current_holder_is_not_current():
    # The proof is from generation 1; generation 2 now holds the worktree.
    assert lease.fence_is_current({"epoch": 1}, 2) is False


def test_a_stale_epoch_with_no_current_holder_is_accepted():
    # No worker owns the worktree now (manual / --resume run is legitimate).
    assert lease.fence_is_current({"epoch": 1}, 0) is True


def test_a_legacy_fence_is_accepted_only_when_unleased():
    assert lease.fence_is_current({"epoch": 0}, 0) is True
    assert lease.fence_is_current({"epoch": 0}, 2) is False
    assert lease.fence_is_current({}, 0) is True


# ---------------------------------------------------------------------------
# Role resolution
# ---------------------------------------------------------------------------

def test_suite_lease_role_maps_gatehouse_worktrees_to_gatekeeper():
    assert git_ops._suite_lease_role(
        "/repo/.kilo/worktrees/the-gatehouse/cogship-281", ""
    ) == "gatekeeper"
    assert git_ops._suite_lease_role(
        "/repo/.kilo/worktrees/gatehouse-042", "cogship-042"
    ) == "gatekeeper"


def test_suite_lease_role_maps_quest_worktrees_to_serf():
    assert git_ops._suite_lease_role(
        "/repo/.kilo/worktrees/q1059-purchases", ""
    ) == "serf"
    assert git_ops._suite_lease_role("/repo/.kilo/worktrees/gatehouse-042", "") == "serf"


# ---------------------------------------------------------------------------
# check_suite_proof fencing
# ---------------------------------------------------------------------------

def test_a_legacy_proof_without_lease_epoch_still_validates(tmp_path):
    repo = _repo(tmp_path)
    proof = _proof(repo)
    assert "lease_epoch" not in proof

    result = git_ops.check_suite_proof(cogship_id="cogship-fence", head_sha=proof["head_sha"], cwd=repo)
    assert result["ok"] is True and result["valid"] is True


def test_a_stale_fenced_proof_is_rejected_and_names_both_epochs(tmp_path):
    repo = _repo(tmp_path)
    _proof(repo, lease_epoch=1, lease_role="gatekeeper", lease_holder="gatekeeper:old")
    # A replacement worker now owns the worktree at epoch 2.
    _seed_lease(repo, "gatekeeper", _final_payload(os.getpid(), holder="gatekeeper:new", epoch=2))

    result = git_ops.check_suite_proof(cogship_id="cogship-fence", head_sha=_head(repo), cwd=repo)
    assert result["ok"] is False and result["valid"] is False
    assert result.get("fence_stale") is True
    assert "epoch 1" in result["reason"]
    assert "epoch 2" in result["reason"]
    assert "superseded" in result["reason"]


def test_a_current_fenced_proof_is_accepted(tmp_path):
    repo = _repo(tmp_path)
    _proof(repo, lease_epoch=2, lease_role="gatekeeper", lease_holder="gatekeeper:new")
    _seed_lease(repo, "gatekeeper", _final_payload(os.getpid(), holder="gatekeeper:new", epoch=2))

    result = git_ops.check_suite_proof(cogship_id="cogship-fence", head_sha=_head(repo), cwd=repo)
    assert result["ok"] is True and result["valid"] is True


def test_a_fenced_proof_with_no_current_holder_is_accepted(tmp_path):
    repo = _repo(tmp_path)
    proof = _proof(repo, lease_epoch=3, lease_role="serf", lease_holder="serf:gone")
    # No lease file: the worker released normally after the run.

    result = git_ops.check_suite_proof(cogship_id="cogship-fence", head_sha=proof["head_sha"], cwd=repo)
    assert result["ok"] is True and result["valid"] is True


def test_run_unified_suite_stamps_the_gatekeeper_fence(tmp_path, monkeypatch):
    worktree = tmp_path / ".kilo" / "worktrees" / "gatehouse-cogship-fence"
    worktree.mkdir(parents=True)
    ok, _, _ = lease.claim(
        worktree, "gatekeeper", holder="gatekeeper:1", pid=os.getpid(),
        pgid=os.getpgid(0), session_id="ses_gk",
    )
    assert ok
    proof_path = tmp_path / "proof.json"
    fake = subprocess.CompletedProcess([], 0, stdout="3 passed in 0.1s\n", stderr="")
    monkeypatch.setattr(git_ops, "provision_env_file", lambda p: False)
    monkeypatch.setattr(git_ops, "_suite_proof_path", lambda *a, **k: proof_path)
    monkeypatch.setattr(git_ops, "_run", lambda *a, **k: {"ok": True, "stdout": "abc123"})
    monkeypatch.setattr(git_ops.subprocess, "run", lambda *a, **k: fake)

    res = git_ops.run_unified_suite(
        worktree, command="python -m pytest -q", cogship_id="cogship-fence",
    )
    assert res["ok"] is True
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    assert proof["lease_epoch"] == 1
    assert proof["lease_role"] == "gatekeeper"
    assert proof["lease_holder"] == "gatekeeper:1"
    assert proof["lease_session_id"] == "ses_gk"

