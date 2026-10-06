"""Tests for the 2026-10-06 gatehouse session-fragility hardening batch:

- watchdog zombie reaping (cogship-281 incident: an unreaped continuation
  child reported alive forever, wedging the watch against its wall cap)
- watchdog fresh-session fallback (cogship-281 incident: "no resolvable
  session id — cannot re-prompt the same session" orphaned the quest)
- suite-runner env-pin propagation to the migration-graph preflight
  (cogship-281 env abort: fresh worktree has no .env; the preflight ran
  un-pinned while the suite command self-pinned via `env VAR=...`)
- runsuite --resume proof idempotence (a successor session re-verifies
  without re-paying a 30-minute suite; stale-head proofs re-run)
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from court import cli
from court.git_ops import _extract_env_prefix


# ---------------------------------------------------------------------------
# Watchdog: zombie reaping
# ---------------------------------------------------------------------------

def _wait_reaped(pid, owned, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline and cli._worker_running(pid, owned):
        time.sleep(0.02)


def test_worker_running_owns_child_reaps_on_exit():
    """A child THIS process spawned is detected as exited AND reaped — no
    zombie left behind (the pid-59680 defunct incident)."""
    owned = set()
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = proc.pid
    owned.add(pid)
    proc.wait(timeout=10)
    _wait_reaped(pid, owned)
    assert not cli._worker_running(pid, owned)
    # The child must not linger as an unreaped zombie: a second waitpid says
    # it was already reaped (raises ChildProcessError on POSIX).
    import os
    with pytest.raises((ChildProcessError, OSError)):
        os.waitpid(pid, os.WNOHANG)


def test_worker_running_foreign_pid_uses_kill_probe():
    """Foreign pids (the dispatch sibling) keep the os.kill probe: a live
    unrelated process reports running; a bogus pid does not."""
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        assert cli._worker_running(live.pid, set()) is True
    finally:
        live.kill()
        live.wait(timeout=5)
    assert cli._worker_running(999_999_999, set()) is False


def test_drain_owned_children_reaps_and_clears():
    owned = set()
    for _ in range(3):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait(timeout=5)
        owned.add(p.pid)
    cli._drain_owned_children(owned)
    assert not owned


# ---------------------------------------------------------------------------
# Watchdog: fresh-session fallback
# ---------------------------------------------------------------------------

def _make_watch_args(**kw):
    defaults = dict(
        pid=None, dir=None, agent="gatekeeper", model=None, session="",
        quest="", task_file="TASK.md", max_attempts=2, attempt=1,
        exclude="", session_resolve_seconds=0, wall_cap=60,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _run_watch_bounded(args, monkeypatch):
    """Run cmd_watch with a wall clock that fires right after the first
    decision, so the loop is exercised exactly once per spawn cycle."""
    monkeypatch.setattr(cli, "WATCH_POLL_SECONDS", 0.01)
    ticks = iter([0] + [10 ** 12] * 200)
    monkeypatch.setattr(cli.time, "time", lambda: next(ticks, 10 ** 12))
    cli.cmd_watch(args)


def test_watch_falls_back_to_fresh_session_when_id_unresolvable(tmp_path, monkeypatch):
    """cogship-281 class: worker session id never resolved; the turn ended
    with quests still GATE. The watchdog must spawn a fresh-session
    continuation (no --session flag) instead of orphaning the quest."""
    (tmp_path / ".kilo").mkdir()
    (tmp_path / ".kilo" / "TASK.md").write_text("Integrate the convoy.", encoding="utf-8")
    fake_quest = MagicMock(status="GATE", id="Q784-X")
    monkeypatch.setattr(cli, "_load_watch_quests", lambda qids, wt: [fake_quest])
    monkeypatch.setattr(cli, "find_kilo_binary", lambda: "/usr/local/bin/kilo")
    monkeypatch.setattr(cli, "query_latest_kilo_session_id", lambda *a, **k: "")
    spawned = []

    class FakeProc:
        def __init__(self):
            self.pid = 424242

    def fake_popen(cmd, **kw):
        spawned.append(cmd)
        return FakeProc()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    _run_watch_bounded(_make_watch_args(dir=str(tmp_path), wall_cap=5), monkeypatch)
    kilo_spawns = [c for c in spawned if "kilo" in str(c[0])]
    assert kilo_spawns, "fresh-session fallback must spawn a continuation"
    cmd = kilo_spawns[0]
    assert "--session" not in cmd
    assert "--agent" in cmd and "gatekeeper" in cmd
    assert "Integrate the convoy." in cmd[-1]
    log = (tmp_path / ".kilo" / "gatekeeper.log").read_text(encoding="utf-8")
    assert "falling back to a fresh-session continuation" in log


def test_watch_reprompts_same_session_when_resolvable(tmp_path, monkeypatch):
    """Unchanged behavior when the session id resolves: re-prompt WITH
    --session (same-session continuation contract preserved)."""
    (tmp_path / ".kilo").mkdir()
    fake_quest = MagicMock(status="GATE", id="Q1")
    monkeypatch.setattr(cli, "_load_watch_quests", lambda qids, wt: [fake_quest])
    monkeypatch.setattr(cli, "find_kilo_binary", lambda: "/usr/local/bin/kilo")
    monkeypatch.setattr(cli, "query_latest_kilo_session_id", lambda *a, **k: "ses_known")
    spawned = []

    class FakeProc:
        pid = 111

    def fake_popen(cmd, **kw):
        spawned.append(cmd)
        return FakeProc()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    _run_watch_bounded(_make_watch_args(dir=str(tmp_path), session="ses_known", wall_cap=5), monkeypatch)
    kilo_spawns = [c for c in spawned if "kilo" in str(c[0])]
    assert kilo_spawns and "--session" in kilo_spawns[0] and "ses_known" in kilo_spawns[0]


def test_watch_no_worktree_and_no_session_is_terminal(tmp_path, monkeypatch):
    """No session id AND no worktree: still terminal (nothing to continue
    in), but logged as the explicit fallback-impossible case."""
    fake_quest = MagicMock(status="WORKING", id="Q1")
    monkeypatch.setattr(cli, "_load_watch_quests", lambda qids, wt: [fake_quest])
    monkeypatch.setattr(cli, "find_kilo_binary", lambda: "/usr/local/bin/kilo")
    monkeypatch.setattr(cli, "query_latest_kilo_session_id", lambda *a, **k: "")
    spawned = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd) or MagicMock(pid=1))
    # dir=None: _watch_log/_watch_write_state fall back to Path.cwd() — point
    # them at a temp dir by chdir-free means (they accept cwd fallback; use a
    # real cwd-independent assertion on the spawn list instead).
    _run_watch_bounded(_make_watch_args(dir=None, wall_cap=5), monkeypatch)
    assert not spawned


# ---------------------------------------------------------------------------
# Suite runner: env-pin propagation
# ---------------------------------------------------------------------------

def test_extract_env_prefix_variants():
    cmd = ("env DATABASE_URL=postgres://x LOCAL_TEST_DB=1 QSTASH_URL=dummy_value "
           "ROLE=web python -m pytest -q")
    pin = _extract_env_prefix(cmd)
    assert pin == {
        "DATABASE_URL": "postgres://x",
        "LOCAL_TEST_DB": "1",
        "QSTASH_URL": "dummy_value",
        "ROLE": "web",
    }
    # Single-token quoted values keep their quotes stripped (matching the
    # runner's plain-split argv semantics — space-containing values are not
    # representable in the pipeline's suite commands).
    assert _extract_env_prefix("env FOO='bar' python -m pytest")["FOO"] == "bar"
    assert _extract_env_prefix("python -m pytest -q") == {}
    assert _extract_env_prefix("") == {}
    # `env -u FOO` (unset flag) stops prefix parsing at the non-assignment.
    assert _extract_env_prefix("env -u FOO python -m pytest") == {}


def _seed_suite_tmp(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir(parents=True)
    (wt / "manage.py").write_text("# django\n", encoding="utf-8")
    (wt / "apps" / "core" / "migrations").mkdir(parents=True)
    proof_dir = tmp_path / "proofs"
    return wt, proof_dir


def test_run_unified_suite_passes_env_pin_to_graph_check(tmp_path, monkeypatch):
    """cogship-281 env abort: the suite command self-pins via `env VAR=...`;
    the migration-graph preflight must run under the SAME pinned env."""
    wt, proof_dir = _seed_suite_tmp(tmp_path)
    seen = {}
    monkeypatch.setattr(
        "court.git_ops.migration_graph.check_migration_graph",
        lambda p, extra_env=None: (seen.__setitem__("extra_env", extra_env),
                                   {"ok": True, "mode": "clean", "reason": ""})[1])
    monkeypatch.setattr("court.git_ops._suite_proof_path",
                        lambda cwd=None, cogship_id="", quest_id="": proof_dir / "s.json")
    monkeypatch.setattr("court.git_ops.subprocess.run",
                        lambda *a, **kw: MagicMock(returncode=0, stdout="7 passed", stderr=""))
    suite_cmd = "env DATABASE_URL=postgres://x QSTASH_URL=dummy_value ROLE=web python -m pytest -q"
    from court.git_ops import run_unified_suite
    res = run_unified_suite(wt, command=suite_cmd, cogship_id="cogship-envpin")
    assert res["ok"] is True
    assert seen["extra_env"] == {
        "DATABASE_URL": "postgres://x",
        "QSTASH_URL": "dummy_value",
        "ROLE": "web",
    }


def test_run_unified_suite_graph_env_abort_uses_parsed_pin(tmp_path, monkeypatch):
    """When the graph check would fail for environment reasons, the pin from
    the suite command reaches it — no more un-pinned preflight aborts."""
    wt, proof_dir = _seed_suite_tmp(tmp_path)
    seen = {}

    def fake_graph(p, extra_env=None):
        seen["extra_env"] = extra_env
        if extra_env and extra_env.get("QSTASH_URL"):
            return {"ok": True, "mode": "clean", "reason": ""}
        return {"ok": False, "mode": "environment", "reason": "graph check could not run",
                "output_tail": "ValueError: QSTASH_URL"}

    monkeypatch.setattr("court.git_ops.migration_graph.check_migration_graph", fake_graph)
    monkeypatch.setattr("court.git_ops._suite_proof_path",
                        lambda cwd=None, cogship_id="", quest_id="": proof_dir / "s.json")
    monkeypatch.setattr("court.git_ops.subprocess.run",
                        lambda *a, **kw: MagicMock(returncode=0, stdout="3 passed", stderr=""))
    monkeypatch.setattr("court.git_ops._detect_suite_command", lambda p: "")
    from court.git_ops import run_unified_suite
    res = run_unified_suite(wt, command="env QSTASH_URL=dummy_value python -m pytest -q",
                            cogship_id="cogship-envpin2")
    assert res["ok"] is True
    assert seen["extra_env"]["QSTASH_URL"] == "dummy_value"


# ---------------------------------------------------------------------------
# runsuite --resume: proof idempotence
# ---------------------------------------------------------------------------

def _runsuite_args(**kw):
    defaults = dict(cogship=None, quest_id=None, dir=None, command=None,
                    timeout=1800, json=False, resume=False)
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_runsuite_resume_with_green_proof_skips_rerun(tmp_path, monkeypatch, capsys):
    """--resume with a GREEN durable proof at the SAME head re-verifies
    WITHOUT re-paying the suite."""
    from court import store

    gatehouse = tmp_path / "gatehouse"
    gatehouse.mkdir()
    quest = MagicMock()
    quest.id = "Q1"
    quest.cogship_id = "cogship-281"
    quest.worktree = str(gatehouse)
    monkeypatch.setattr(store, "list_all", lambda: [quest])
    monkeypatch.setattr(store, "normalize_cogship_id", lambda x: x)

    proof = tmp_path / "proofs" / "cogship-281.json"
    proof.parent.mkdir(parents=True)
    proof.write_text(json.dumps({
        "ok": True, "exit_code": 0, "ran_tests": 11176,
        "head_sha": "abc123", "proof_path": str(proof),
    }), encoding="utf-8")
    monkeypatch.setattr("court.git_ops._suite_proof_path",
                        lambda cwd=None, cogship_id="", quest_id="": proof)
    monkeypatch.setattr("court.git_ops._run",
                        lambda argv, cwd=None, **kw: {"ok": True, "stdout": "abc123\n"})
    called = {"n": 0}
    monkeypatch.setattr("court.git_ops.run_unified_suite",
                        lambda *a, **kw: called.__setitem__("n", called["n"] + 1) or {})

    cli.cmd_runsuite(_runsuite_args(cogship="cogship-281", resume=True, json=True))
    out = capsys.readouterr().out
    assert called["n"] == 0
    assert "nothing to re-run" in out
    assert "11176" in out
    assert json.loads(out[out.index("{"):])["resumed"] is True


def test_runsuite_resume_stale_head_reruns(tmp_path, monkeypatch, capsys):
    """A green proof stamped for an OLDER head is stale by definition —
    --resume must re-run rather than bless the wrong tree."""
    from court import store

    gatehouse = tmp_path / "gatehouse"
    gatehouse.mkdir()
    quest = MagicMock()
    quest.id = "Q1"
    quest.cogship_id = "cogship-282"
    quest.worktree = str(gatehouse)
    monkeypatch.setattr(store, "list_all", lambda: [quest])
    monkeypatch.setattr(store, "normalize_cogship_id", lambda x: x)

    proof = tmp_path / "proofs" / "cogship-282.json"
    proof.parent.mkdir(parents=True)
    proof.write_text(json.dumps({
        "ok": True, "exit_code": 0, "ran_tests": 9000,
        "head_sha": "oldsha", "proof_path": str(proof),
    }), encoding="utf-8")
    monkeypatch.setattr("court.git_ops._suite_proof_path",
                        lambda cwd=None, cogship_id="", quest_id="": proof)
    monkeypatch.setattr("court.git_ops._run",
                        lambda argv, cwd=None, **kw: {"ok": True, "stdout": "newsha\n"})
    called = {"n": 0}
    monkeypatch.setattr("court.git_ops.run_unified_suite",
                        lambda *a, **kw: called.__setitem__("n", called["n"] + 1) or {"ok": True})

    cli.cmd_runsuite(_runsuite_args(cogship="cogship-282", dir=str(gatehouse), resume=True))
    out = capsys.readouterr().out
    assert called["n"] == 1
    assert "re-running" in out


# ---------------------------------------------------------------------------
# Suite runner: .env provisioning for fresh worktrees (cogship-281 outbreak)
# ---------------------------------------------------------------------------

def test_run_unified_suite_copies_root_env_into_fresh_worktree(tmp_path, monkeypatch):
    """A gatehouse worktree without .env gets the primary checkout's copy
    before the suite runs; an existing .env is never overwritten."""
    from court.git_ops import provision_env_file, run_unified_suite

    root = tmp_path / "root"
    root.mkdir()
    (root / ".env").write_text("FERNET_KEYS=abc\nQSTASH_URL=dummy_value\n", encoding="utf-8")
    wt = tmp_path / "gatehouse"
    wt.mkdir()

    monkeypatch.setattr("court.git_ops._primary_repo_root_for", lambda p: root)
    assert provision_env_file(wt) is True
    assert (wt / ".env").read_text(encoding="utf-8").startswith("FERNET_KEYS=abc")
    # Idempotent: second call does not touch the existing file.
    (wt / ".env").write_text("FERNET_KEYS=kept\n", encoding="utf-8")
    assert provision_env_file(wt) is False
    assert (wt / ".env").read_text(encoding="utf-8") == "FERNET_KEYS=kept\n"

    # And the suite runner provisions automatically.
    (wt / ".env").unlink()
    seen = {}
    monkeypatch.setattr("court.git_ops.migration_graph.check_migration_graph",
                        lambda p, extra_env=None: {"ok": True, "mode": "clean", "reason": ""})
    monkeypatch.setattr("court.git_ops._suite_proof_path",
                        lambda cwd=None, cogship_id="", quest_id="": tmp_path / "proof.json")
    monkeypatch.setattr("court.git_ops.subprocess.run",
                        lambda *a, **kw: MagicMock(returncode=0, stdout="5 passed", stderr=""))
    monkeypatch.setattr("court.git_ops._detect_suite_command", lambda p: "")
    run_unified_suite(wt, command="python -m pytest -q", cogship_id="cogship-envprov")
    assert (wt / ".env").exists()


def test_provision_env_noop_without_root_env(tmp_path, monkeypatch):
    from court.git_ops import provision_env_file
    root = tmp_path / "root"
    root.mkdir()  # no .env in the primary root
    wt = tmp_path / "gatehouse"
    wt.mkdir()
    monkeypatch.setattr("court.git_ops._primary_repo_root_for", lambda p: root)
    assert provision_env_file(wt) is False
    assert not (wt / ".env").exists()
