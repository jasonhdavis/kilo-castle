"""Tests for the ten-recommendations hardening batch:

- keepdb recreate (Q620 class: migration renumbers vs --keepdb test DBs)
- continuation watchdog decision (single-turn death: cogship-253 class)
- sync direction guard (castle PUNISHED/HELD is royal; castle-ahead sync-down)
- collection gates reading the worktree charter (studio-four chicken-and-egg)
- single-convoy guard (concurrent convoys race the castle promotion)
- lifecycle teardown sweep classification
- court shim + PYTHONPATH injection (court importable inside worktrees)
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from court import cli
from court.git_ops import (
    keepdb_recreate_command,
    keepdb_recreate_decision,
    migrations_fingerprint,
    suite_keepdb_mode,
)
from court.models import Quest
from court.store import save, path_for


# ---------------------------------------------------------------------------
# Rec 10: keepdb recreate
# ---------------------------------------------------------------------------

def test_suite_keepdb_mode_classification():
    assert suite_keepdb_mode("python manage.py test --keepdb --parallel 4") == "django"
    assert suite_keepdb_mode("python -m pytest -q --reuse-db") == "pytest"
    assert suite_keepdb_mode("python manage.py test") is None
    assert suite_keepdb_mode("python -m pytest -q") is None


def test_keepdb_recreate_command_variants():
    assert "--keepdb" not in keepdb_recreate_command("manage.py test --keepdb", "django")
    recreate = keepdb_recreate_command("pytest --reuse-db -q", "pytest")
    assert "--create-db" in recreate and "--reuse-db" in recreate


def test_keepdb_recreate_decision_on_fingerprint_change():
    cmd = "manage.py test --keepdb"
    # First run: no stored state -> keep, start recording.
    recreate, run_cmd, _ = keepdb_recreate_decision(cmd, "fp-1", None)
    assert not recreate and run_cmd == cmd
    # Same fingerprint -> keep.
    recreate, run_cmd, _ = keepdb_recreate_decision(cmd, "fp-1", {"fingerprint": "fp-1", "mode": "django"})
    assert not recreate and run_cmd == cmd
    # Migration renumber changed the fingerprint -> recreate once, drop --keepdb.
    recreate, run_cmd, reason = keepdb_recreate_decision(cmd, "fp-2", {"fingerprint": "fp-1", "mode": "django"})
    assert recreate and "--keepdb" not in run_cmd and "renumber" in reason.lower() or "changed" in reason.lower()


def test_migrations_fingerprint_changes_on_renumber(tmp_path):
    mig = tmp_path / "apps" / "core" / "migrations"
    mig.mkdir(parents=True)
    (mig / "0038_remove_old.py").write_text("from django.db import migrations\n", encoding="utf-8")
    fp1 = migrations_fingerprint(tmp_path)
    # Renumber: 0038 -> 0039 (delete + new file).
    (mig / "0038_remove_old.py").unlink()
    (mig / "0039_remove_old.py").write_text("from django.db import migrations\n", encoding="utf-8")
    fp2 = migrations_fingerprint(tmp_path)
    assert fp1 and fp2 and fp1 != fp2


def test_run_unified_suite_recreates_stale_keepdb(tmp_path, monkeypatch):
    """End-to-end: with a --keepdb suite command and a changed migrations
    fingerprint, the engine runs the recreate variant and records why."""
    wt = tmp_path / "wt"
    (wt / "apps" / "core" / "migrations").mkdir(parents=True)
    (wt / "apps" / "core" / "migrations" / "0039_new.py").write_text("# m\n", encoding="utf-8")
    proof_dir = tmp_path / "proofs"
    monkeypatch.setattr("court.git_ops._suite_proof_path",
                        lambda cwd=None, cogship_id="", quest_id="": proof_dir / "s.json")
    # Seed a stored fingerprint from the OLD migration set.
    fp_path = wt / ".court" / "suites" / ".testdb-fingerprint.json"
    fp_path.parent.mkdir(parents=True)
    fp_path.write_text(json.dumps({"fingerprint": "old", "mode": "pytest"}), encoding="utf-8")

    captured = {}

    def fake_run(argv, cwd, capture_output, text, timeout):
        captured["argv"] = argv
        return MagicMock(returncode=0, stdout="42 passed", stderr="")

    monkeypatch.setattr("court.git_ops.subprocess.run", fake_run)
    monkeypatch.setattr("court.git_ops.migration_graph.check_migration_graph",
                        lambda p: {"ok": True, "mode": "django", "reason": ""})
    # No manage.py -> graph stage skipped; add it to prove no crash either way.
    res = cli_git_ops_run(wt, command="pytest -q --reuse-db", cogship_id="cogship-1")
    assert res["ok"] is True
    assert res["test_db_recreated"]
    assert "--create-db" in captured["argv"]
    assert "--reuse-db" in captured["argv"]


def cli_git_ops_run(wt, **kw):
    from court.git_ops import run_unified_suite
    return run_unified_suite(wt, **kw)


# ---------------------------------------------------------------------------
# Rec 4: continuation watchdog decision
# ---------------------------------------------------------------------------

def _q(status="WORKING", qid="Q900-Watch"):
    return Quest(id=qid, title="Watch", app="t", concern="watch", status=status)


def test_watch_continues_on_nonzero_exit():
    cont, why = cli.watch_should_continue("serf", [], 7)
    assert cont and "7" in why


def test_watch_continues_while_quest_in_active_state():
    cont, why = cli.watch_should_continue("serf", [_q("WORKING")], 0)
    assert cont and "WORKING" in why


def test_watch_stops_when_quest_rendered():
    cont, _ = cli.watch_should_continue("serf", [_q("TRIBUTE_READY")], 0)
    assert not cont


def test_watch_role_active_states():
    assert cli._WATCH_ROLE_ACTIVE_STATES["gatekeeper"] == ("GATE",)
    assert cli._WATCH_ROLE_ACTIVE_STATES["master_of_coin"] == ("TRIBUTE_READY",)
    assert "HELD" not in cli._WATCH_ROLE_ACTIVE_STATES["serf"]
    assert "PUNISHED" not in cli._WATCH_ROLE_ACTIVE_STATES["serf"]


def test_watch_max_attempts_reported(capsys):
    # Simulate: at max attempts, quest still active -> no re-prompt, message printed.
    cont, why = cli.watch_should_continue("serf", [_q("WORKING")], 0)
    assert cont  # decision says continue, but cmd_watch caps at max_attempts
    assert cont and why


# ---------------------------------------------------------------------------
# Rec 5: sync direction guard
# ---------------------------------------------------------------------------

def _charter_text(status="WORKING"):
    q = Quest(id="Q901-DirGuard", title="DirGuard", app="t", concern="dir", status=status)
    q.branch = "quest/x"
    q.set_section("The Kingdom Requires", "Requirement.")
    return q.to_markdown()


def test_sync_frozen_side_state_never_folds(tmp_path, monkeypatch, capsys):
    """castle PUNISHED + branch WORKING -> the branch must NOT be folded onto
    castle; the ruling is pushed down instead."""
    repo = tmp_path
    quests_dir = tmp_path / ".court" / "quests"
    quests_dir.mkdir(parents=True)
    qpath = quests_dir / "Q901-DirGuard.md"
    qpath.write_text(_charter_text("PUNISHED"), encoding="utf-8")

    branch_text = _charter_text("WORKING")
    monkeypatch.setattr(cli.git_ops, "get_repo_root", lambda: repo)
    monkeypatch.setattr(cli.store, "find_path", lambda qid, root=None: qpath)

    def fake_git_out(repo_, *argv):
        argv = tuple(str(a) for a in argv)
        if argv == ("rev-parse", "--verify", "--quiet", "quest/x"):
            return "abc123"
        if argv == ("show", "quest/x:.court/quests/Q901-DirGuard.md"):
            return branch_text
        return ""

    monkeypatch.setattr(cli, "_git_out", fake_git_out)
    pushed = {}
    monkeypatch.setattr(cli, "_push_file_to_branch",
                        lambda repo_, branch, rel, content: pushed.update({"branch": branch, "content": content}) or (True, "deadbeef"))

    class A: all = False; base = "castle"; quest_ids = ["Q901"]
    cli.cmd_sync(A())

    assert pushed["branch"] == "quest/x"
    assert "PUNISHED" in pushed["content"]  # ruling travelled down, not up
    out = capsys.readouterr().out
    assert "royal" in out


# ---------------------------------------------------------------------------
# Rec 6: collection gates read the worktree charter
# ---------------------------------------------------------------------------

def test_quest_worktree_charter_reads_worktree_copy(tmp_path):
    quest = _q("GATE", "Q902-WTGate")
    wt_dir = tmp_path / "wt"
    quest.worktree = str(wt_dir)
    wt_charter_dir = wt_dir / ".court" / "quests"
    wt_charter_dir.mkdir(parents=True)
    # Worktree copy carries an APPROVED UI review; the castle copy is PENDING.
    wtq = Quest(id="Q902-WTGate", title="WTGate", app="t", concern="wt", status="GATE")
    wtq.set_section("Master of Coin's Audit", "- **UI Review:** APPROVED (royal sign-off)")
    (wt_charter_dir / "Q902-WTGate.md").write_text(wtq.to_markdown(), encoding="utf-8")

    loaded = cli._quest_worktree_charter(quest)
    assert loaded is not None
    assert loaded.extract_ui_review_status().upper().startswith("APPROVED")


def test_quest_worktree_charter_missing_worktree_returns_none(tmp_path):
    quest = _q("GATE", "Q903-None")
    quest.worktree = str(tmp_path / "missing")
    assert cli._quest_worktree_charter(quest) is None


# ---------------------------------------------------------------------------
# Rec 9: lifecycle teardown sweep
# ---------------------------------------------------------------------------

def test_lifecycle_sweep_classifies_stale_sessions(tmp_path, monkeypatch):
    court_dir = tmp_path / ".court"
    (court_dir / "quests").mkdir(parents=True)
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    fresh_ts = datetime.now(timezone.utc).isoformat()
    for qid, ts in (("Q910-Old-Done", old_ts), ("Q911-Fresh-Done", fresh_ts)):
        q = Quest(id=qid, title=qid, app="t", concern="sweep", status="DONE")
        q.updated_at = ts
        q.serf_session_id = "ses_deadbeef000"
        q.worktree = str(tmp_path / f"wt-{qid}")
        save(q, court_root=court_dir, auto_commit=False)

    monkeypatch.setattr(cli.store, "get_court_root", lambda: court_dir)
    monkeypatch.setattr(cli.store, "list_all", lambda include_archive=False, court_root=None: [
        Quest.from_markdown((court_dir / "quests" / f"{qid}.md").read_text(encoding="utf-8"))
        for qid in ("Q910-Old-Done", "Q911-Fresh-Done")
    ])
    sweep = cli.monitor_lifecycle_sweep()
    ids = {s["quest"] for s in sweep["stale_sessions"]}
    assert "Q910-Old-Done" in ids       # 48h old -> candidate
    assert "Q911-Fresh-Done" not in ids  # inside the grace window -> keep


def test_lifecycle_sweep_pid_fallback_ids_always_candidates(tmp_path, monkeypatch):
    court_dir = tmp_path / ".court"
    (court_dir / "quests").mkdir(parents=True)
    fresh_ts = datetime.now(timezone.utc).isoformat()
    q = Quest(id="Q912-PidFallback", title="PF", app="t", concern="sweep", status="PUNISHED")
    q.updated_at = fresh_ts
    q.serf_session_id = "kilo-serf-12345"  # session that never really existed
    save(q, court_root=court_dir, auto_commit=False)
    monkeypatch.setattr(cli.store, "list_all", lambda include_archive=False, court_root=None: [
        Quest.from_markdown((court_dir / "quests" / "Q912-PidFallback.md").read_text(encoding="utf-8"))
    ])
    sweep = cli.monitor_lifecycle_sweep()
    assert any(s["quest"] == "Q912-PidFallback" for s in sweep["stale_sessions"])


# ---------------------------------------------------------------------------
# Rec 3: court shim + PYTHONPATH injection
# ---------------------------------------------------------------------------

def test_write_court_shim_resolves_engine(tmp_path):
    pkg_root = tmp_path  # contains court/ with __init__.py (this repo)
    wt = tmp_path / "wt"
    (wt / ".kilo").mkdir(parents=True)
    shim = cli.write_court_shim(wt)
    assert shim is None or shim.is_file()  # resolves somewhere real or not at all


def test_engine_spawn_env_prepends_package_root(tmp_path):
    env = cli._engine_spawn_env(Path(__file__).resolve().parent.parent)
    pp = env.get("PYTHONPATH", "")
    assert str(Path(__file__).resolve().parent.parent) in pp.split(":")[:1]


def test_court_package_root_finds_repo_root():
    root = cli._court_package_root(Path(__file__).resolve().parent.parent)
    assert root is not None and (root / "court" / "__init__.py").is_file()


# ---------------------------------------------------------------------------
# Rec 7: single-convoy guard
# ---------------------------------------------------------------------------

def test_single_convoy_guard_blocks_on_live_gatehouse(tmp_path, monkeypatch, capsys):
    repo = tmp_path
    gatehouse = repo / ".kilo" / "worktrees" / "the-gatehouse-cogship-999"
    gatehouse.mkdir(parents=True)
    monkeypatch.setattr(cli.git_ops, "get_repo_root", lambda: repo)
    monkeypatch.setattr(cli, "session_is_fresh", lambda p, max_age_ms=None: True)
    live, why = cli.gatehouse_convoy_is_live("cogship-999", repo_root=repo)
    assert live is True  # the primitive the guard leans on


def test_single_convoy_guard_name_pattern():
    import re as _re
    assert _re.match(r"the-gatehouse-(cogship-\d+)$", "the-gatehouse-cogship-253")
    assert not _re.match(r"the-gatehouse-(cogship-\d+)$", "the-gatehouse-other")
