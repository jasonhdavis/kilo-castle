"""Pipeline hardening guards (2026-09-28 round).

Covers the incident classes from the hardening survey:
- Class 2: TRIBUTE_READY advance guard (Q277 self-advanced empty tribute).
- Class 2: dispatch on CHARTERED quests (Q695 stale status).
- Class 3: stamp_cogship cross-stamp refusal + op lock (cogship-040/041,
  cogship-247/248, v1332 double-ship).
- Class 4: --auto on every headless kilo run launcher (Q700 permission wall).
- Class 4: detached spawn liveness verification.
"""

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from court import cli, store
from court.cli import main


def _init_git_repo(repo_dir: Path, branch: str = "castle") -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo_dir)], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
    (repo_dir / "README.md").write_text("test repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "initial"], check=True)


@pytest.fixture
def court_repo(tmp_path, monkeypatch):
    _init_git_repo(tmp_path, branch="castle")
    monkeypatch.setenv("COURT_DIR", str(tmp_path / ".court"))
    monkeypatch.chdir(tmp_path)
    main(["init"])
    main([
        "new",
        "--app", "core",
        "--concern", "hardening-probe",
        "--title", "Hardening probe quest",
        "--section", "Bug fix",
        "--goal", "Probe the guards",
        "--tribute", "- [ ] Tests pass",
    ])
    return tmp_path


QUEST = "Q001-Core-Hardening-Probe"


def _quest():
    return store.load(QUEST)


def _render_full_tribute(q: store.Quest, kind: str = "serf") -> None:
    sections = (
        ("Ballad", "Story of the work."),
        ("Tribute", "Tests passed: 233 passed."),
        ("Tally", "5 files changed."),
        ("Penance", "None."),
        ("Audience", "None pending."),
        ("Opinion", "Clean."),
    ) if kind == "serf" else (
        ("Survey", "Terrain mapped."),
        ("Map", "Findings filed."),
        ("Dangers", "Two risks noted."),
        ("Tribute", "Spike code committed."),
        ("Plot", "Next steps listed."),
    )
    for name, body in sections:
        q.set_section("Tribute Rendered", f"### {name}\n{body}", mode="append")
    # Tick the Expected Tribute checklist box.
    et = q.body_sections.get("Expected Tribute", "").replace("- [ ]", "- [x]")
    q.set_section("Expected Tribute", et, mode="replace")


class TestTributeReadyGuard:
    def test_blocked_on_empty_tribute(self, court_repo, capsys):
        main(["advance", QUEST, "WORKING", "--note", "toiling"])
        capsys.readouterr()
        with pytest.raises(SystemExit) as ei:
            main(["advance", QUEST, "TRIBUTE_READY", "--note", "self-advance"])
        assert ei.value.code == 1
        err = capsys.readouterr().err
        assert "Cannot advance" in err
        assert "Tribute Rendered is empty" in err
        # Status must be untouched.
        assert _quest().status == "WORKING"

    def test_blocked_on_incomplete_tribute(self, court_repo, capsys):
        q = _quest()
        q.set_section("Tribute Rendered", "### Ballad\nPartial only.", mode="replace")
        store.save(q)
        with pytest.raises(SystemExit):
            main(["advance", QUEST, "TRIBUTE_READY", "--note", "partial"])
        err = capsys.readouterr().err
        assert "missing required subsection" in err

    def test_blocked_on_unchecked_checklist(self, court_repo, capsys):
        q = _quest()
        _render_full_tribute(q)
        # Restore the unticked checklist so the checklist blocker fires
        # independently of the worktree blocker.
        et = q.body_sections.get("Expected Tribute", "").replace("- [x]", "- [ ]")
        q.set_section("Expected Tribute", et, mode="replace")
        store.save(q)
        with pytest.raises(SystemExit):
            main(["advance", QUEST, "TRIBUTE_READY", "--note", "boxes unticked"])
        err = capsys.readouterr().err
        assert "unchecked item(s)" in err

    def test_hotfix_exempt_from_checklist_blocker(self, court_repo):
        main(["advance", QUEST, "WORKING", "--note", "hotfix toiling"])
        q = _quest()
        q.tags = "Bug fix,hotfix"
        # Render only the six required tribute sections with old-style
        # numbered headers (the hotfix test idiom); checklist stays unticked.
        for name, body in (
            ("1. Ballad", "Fixed it."),
            ("2. Tribute", "webhook.py"),
            ("3. Tally", "tests ok"),
            ("4. Penance", "None"),
            ("5. Audience", "None"),
            ("6. Humble Opinion", "Ship"),
        ):
            q.set_section("Tribute Rendered", f"### {name}\n{body}", mode="append")
        # Resolve the worktree blockers with a clean standalone repo on the
        # matching branch, so only the checklist dimension is exercised.
        wt = court_repo / "wt-hotfix"
        wt.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "castle", str(wt)], check=True)
        subprocess.run(["git", "-C", str(wt), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(wt), "config", "user.name", "T"], check=True)
        (wt / "webhook.py").write_text("X = True\n")
        subprocess.run(["git", "-C", str(wt), "add", "."], check=True)
        subprocess.run(["git", "-C", str(wt), "commit", "-qm", "fix"], check=True)
        q.worktree = str(wt)
        q.branch = "castle"
        store.save(q)
        main(["advance", QUEST, "TRIBUTE_READY", "--note", "hotfix fast-track"])
        assert _quest().status == "TRIBUTE_READY"

    def test_force_overrides_and_marks_ledger(self, court_repo):
        main(["advance", QUEST, "WORKING", "--note", "toiling"])
        main(["advance", QUEST, "TRIBUTE_READY", "--note", "royal override", "--force"])
        q = _quest()
        assert q.status == "TRIBUTE_READY"
        joined = q.body_sections.get("Castle Ledger", "")
        assert "FORCED" in joined
        assert "TRIBUTE-READINESS BLOCKER" in joined

    def test_compliant_passes(self, court_repo):
        main(["advance", QUEST, "WORKING", "--note", "toiling"])
        q = _quest()
        # Scouts skip the worktree-required blocker (no code tree needed), so
        # the compliant path can be exercised without a git worktree fixture.
        q.kind = "scout"
        q.section = "Investigation"
        _render_full_tribute(q, kind="scout")
        et = q.body_sections.get("Expected Tribute", "").replace("- [ ]", "- [x]")
        q.set_section("Expected Tribute", et, mode="replace")
        store.save(q)
        main(["advance", QUEST, "TRIBUTE_READY", "--note", "clean scout report"])
        assert _quest().status == "TRIBUTE_READY"


class TestDispatchChartered:
    def test_chartered_dispatch_advances_to_working(self, court_repo):
        q = _quest()
        q.set_status("CHARTERED", "charter written")
        store.save(q)
        # --create-worktree keeps the flow off the kilo binary and exercises
        # only the status-transition logic under test.
        import argparse
        args = argparse.Namespace(
            quest_id=QUEST, branch=None, worktree=str(court_repo / "wt-probe"),
            create_worktree=True, native=False, standup=False, no_run=True,
            prompt=None, session_id=None, agent=None, serf_model=None,
            base="castle", no_commit=True, json=False, status=None, app=None,
            epic=None, kind=None, tag=None, quest_ids=None, force=False,
        )
        Path(args.worktree).mkdir(exist_ok=True)
        with mock.patch.object(cli, "standup_kilo_session", return_value={"ok": True, "mode": "none", "session_id": "ses_test", "message": ""}):
            cli.cmd_dispatch(args)
        q = _quest()
        assert q.status == "WORKING"
        joined = q.body_sections.get("Castle Ledger", "")
        assert "dispatched" in joined


class TestStampGuard:
    def test_cross_stamp_refused(self, court_repo):
        q = _quest()
        q.cogship_id = "cogship-001"
        store.save(q)
        with pytest.raises(ValueError, match="Refusing to stamp"):
            store.stamp_cogship([q], cogship_id="cogship-002", auto_commit=False)

    def test_cross_stamp_same_id_allowed(self, court_repo):
        q = _quest()
        q.cogship_id = "cogship-001"
        store.save(q)
        out_id = store.stamp_cogship([q], cogship_id="cogship-001", auto_commit=False)
        assert out_id == "cogship-001"

    def test_force_repack_allowed(self, court_repo):
        q = _quest()
        q.cogship_id = "cogship-001"
        store.save(q)
        out_id = store.stamp_cogship([q], cogship_id="cogship-002", auto_commit=False, force=True)
        assert out_id == "cogship-002"


class TestOpLock:
    def test_lock_is_exclusive(self, tmp_path, monkeypatch):
        lock_dir = tmp_path / "locks"
        monkeypatch.setattr(cli, "_OP_LOCK_DIR", lock_dir)
        p = cli._op_lock_path("probe")
        assert cli.acquire_op_lock("probe") is True
        assert cli.acquire_op_lock("probe") is True  # same-process re-entry
        lock_path = cli._op_lock_path("probe")
        lock_path.write_text("1")  # simulate a different holder pid
        assert cli.acquire_op_lock("probe") is False  # fresh foreign lock held
        assert cli.acquire_op_lock("other") is True

    def test_require_lock_exits(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_OP_LOCK_DIR", tmp_path / "locks")
        cli.require_op_lock("probe")
        capsys.readouterr()
        # Same process: idempotent.
        cli.require_op_lock("probe")
        # Simulate a concurrent LIVE foreign holder (pid 1 always exists).
        lock_path = cli._op_lock_path("probe")
        lock_path.write_text("1")
        with pytest.raises(SystemExit):
            cli.require_op_lock("probe")
        assert "already in progress" in capsys.readouterr().err

    def test_stale_lock_stolen(self, tmp_path, monkeypatch):
        import os
        import time
        monkeypatch.setattr(cli, "_OP_LOCK_DIR", tmp_path / "locks")
        lock_path = cli._op_lock_path("probe")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("999999")  # pid that cannot exist
        old = time.time() - 100000
        os.utime(lock_path, (old, old))
        assert cli.acquire_op_lock("probe") is True

    def test_ship_lock_blocks_second_process_run(self, court_repo, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_OP_LOCK_DIR", court_repo / ".locks")
        # Hold the lock as a LIVE foreign pid (1 always exists).
        lock_path = cli._op_lock_path("ship")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("1")
        with pytest.raises(SystemExit):
            main(["ship"])
        assert "already in progress" in capsys.readouterr().err


class TestAutoFlag:
    def test_standup_cmd_includes_auto(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COURT_WATCH_DISABLE", "1")
        launched = {}

        class FakeProc:
            pid = 4242
            def poll(self):
                return None

        def fake_popen(cmd, **kwargs):
            launched["cmd"] = cmd
            return FakeProc()

        monkeypatch.setattr(cli, "find_kilo_binary", lambda: Path("/fake/kilo"))
        monkeypatch.setattr(cli, "query_kilo_session_ids", lambda p: set())
        monkeypatch.setattr(cli, "query_latest_kilo_session_id", lambda *a, **k: "ses_fake")
        monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
        res = cli.standup_kilo_session(
            worktree_path=tmp_path,
            agent="serf",
            model="z-ai/glm-5.3-flash",
            prompt="do the work",
            title="probe",
            kilo_bin=Path("/fake/kilo"),
            run_now=True,
        )
        assert res["ok"] is True
        assert "--auto" in launched["cmd"]
        # Prompt must remain the last positional argument after --auto.
        assert launched["cmd"][-1] == "do the work"

    def test_coin_cmd_includes_auto(self, court_repo, monkeypatch):
        captured = []

        class FakeProc:
            pid = 4242
            def poll(self):
                return None

        monkeypatch.setattr(cli, "find_kilo_binary", lambda: Path("/fake/kilo"))
        monkeypatch.setattr(cli, "query_kilo_session_ids", lambda p: set())
        monkeypatch.setattr(cli, "query_latest_kilo_session_id", lambda *a, **k: "ses_coin")
        # store.save autocommit also Popens git; keep only kilo launches.
        monkeypatch.setattr(
            cli.subprocess, "Popen",
            lambda cmd, **kw: (captured.append(list(cmd)) or FakeProc())
            if "kilo" in str(cmd[0]) else FakeProc(),
        )
        q = _quest()
        q.set_status("TRIBUTE_READY", "ready")
        q.worktree = str(court_repo)
        store.save(q)
        # Invoke cmd_coin directly with a stubbed args namespace.
        import argparse
        args = argparse.Namespace(
            quest_id=QUEST, model=None, force=False, wait=False, no_commit=True,
        )
        cli.cmd_coin(args)
        kilo_cmds = [c for c in captured if "kilo" in str(c[0])]
        assert kilo_cmds and "--auto" in kilo_cmds[-1]
        assert "master_of_coin" in kilo_cmds[-1]

    def test_goad_cmd_includes_auto(self, court_repo, monkeypatch):
        captured = []

        class FakeProc:
            pid = 4242
            def poll(self):
                return None

        monkeypatch.setattr(cli, "find_kilo_binary", lambda: Path("/fake/kilo"))
        monkeypatch.setattr(cli, "query_kilo_session_ids", lambda p: set())
        monkeypatch.setattr(cli, "query_latest_kilo_session_id", lambda *a, **k: "ses_goad")
        monkeypatch.setattr(
            cli.subprocess, "Popen",
            lambda cmd, **kw: (captured.append(list(cmd)) or FakeProc())
            if "kilo" in str(cmd[0]) else FakeProc(),
        )
        q = _quest()
        q.worktree = str(court_repo)
        store.save(q)
        main(["goad", QUEST])
        kilo_cmds = [c for c in captured if "kilo" in str(c[0])]
        assert kilo_cmds and "--auto" in kilo_cmds[-1]


class TestSpawnLiveness:
    def test_standup_reports_dead_spawn(self, tmp_path, monkeypatch):
        class DeadProc:
            pid = 4242
            def poll(self):
                return 1  # exited

        monkeypatch.setattr(cli, "find_kilo_binary", lambda: Path("/fake/kilo"))
        monkeypatch.setattr(cli, "query_kilo_session_ids", lambda p: set())
        monkeypatch.setattr(cli, "query_latest_kilo_session_id", lambda *a, **k: None)
        monkeypatch.setattr(cli.subprocess, "Popen", lambda cmd, **kw: DeadProc())
        res = cli.standup_kilo_session(
            worktree_path=tmp_path,
            agent="serf",
            model="z-ai/glm-5.3-flash",
            prompt="do the work",
            title="probe",
            kilo_bin=Path("/fake/kilo"),
            run_now=True,
        )
        assert res["ok"] is False
        assert res["mode"] == "dead-spawn"
