"""Tests for the 2026-10-06 worker lease + stall referee (Watchdog v2):

- lease claim/refuse/steal/supersede semantics (Q799 stack class: a live
  dispatch Serf, a manual goad, and a console "continue" stacked three
  concurrent agents onto ONE worktree — three writers racing one branch)
- log-liveness verdicts feeding the dashboard and the stall referee
- watchdog v2: stall referee kills a hung worker (owned or ps-verified
  foreign) then re-prompts through the normal continuation path; a live
  foreign lease holder suppresses duplicate continuations
- standup/goad/coin spawn paths refuse to spawn while a worker lease is live
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from court import cli, lease


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sleeper(seconds=30):
    """A worker stand-in: its own process group (start_new_session), command
    line containing 'python' (passes the pid-reuse trust check)."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_gone(pid, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if lease._worker_down(pid):
            return True
        time.sleep(0.02)
    return False


def _seed_lease(tmp_path, role, payload):
    p = lease.lease_path(tmp_path, role)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(__import__("json").dumps(payload), encoding="utf-8")
    return p


def _final_payload(pid, pgid=None, holder="dispatch", session_id="ses_1", watchdog_pid=None):
    return {
        "v": 1, "pending": False, "pid": pid, "pgid": pgid if pgid is not None else pid,
        "session_id": session_id, "watchdog_pid": watchdog_pid, "holder": holder,
        "model": "m", "started": time.time(), "heartbeat": time.time(),
    }


def _kilo_spawns(spawned):
    """Filter a global-Popen recorder down to actual kilo worker spawns —
    the patch is on the shared subprocess module, so internal ps/git probes
    from lease/ward land in the same list."""
    out = []
    for entry in spawned:
        argv = entry[0] if isinstance(entry, tuple) else entry
        if "kilo" in str(argv[0] if argv else ""):
            out.append(argv)
    return out


def _make_watch_args(**kw):
    defaults = dict(
        pid=None, pgid=None, dir=None, agent="serf", model=None, session="",
        quest="", task_file="TASK.md", max_attempts=2, attempt=1,
        exclude="", session_resolve_seconds=0, wall_cap=60, stall_seconds=0,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _run_watch_bounded(args, monkeypatch, ticks=None):
    """Run cmd_watch with a fake wall clock so the loop is exercised exactly
    once per spawn cycle (unless a custom tick sequence is given)."""
    monkeypatch.setattr(cli, "WATCH_POLL_SECONDS", 0.01)
    if ticks is None:
        ticks = [0] + [10 ** 12] * 200
    seq = iter(ticks)
    monkeypatch.setattr(cli.time, "time", lambda: next(seq, 10 ** 12))
    cli.cmd_watch(args)


# ---------------------------------------------------------------------------
# Lease claim / refuse / steal / supersede
# ---------------------------------------------------------------------------

def test_claim_creates_final_lease(tmp_path):
    ok, reason, _ = lease.claim(tmp_path, "serf", holder="dispatch", pid=4242, pgid=4242, session_id="ses_a")
    assert ok and reason == "claimed"
    payload = lease.read_lease(tmp_path, "serf")
    assert payload["pid"] == 4242 and payload["session_id"] == "ses_a"
    assert payload["pending"] is False and payload["holder"] == "dispatch"


def test_claim_refused_while_holder_live(tmp_path):
    s = _sleeper()
    try:
        _seed_lease(tmp_path, "serf", _final_payload(s.pid, pgid=os.getpgid(s.pid)))
        ok, reason, prev = lease.claim(tmp_path, "serf", holder="goad", pending=True)
        assert not ok
        assert reason.startswith("refused-live")
        assert prev["pid"] == s.pid
        assert lease._pid_alive(s.pid)  # holder untouched
    finally:
        s.kill(); s.wait(timeout=5)


def test_claim_steals_dead_holder(tmp_path):
    _seed_lease(tmp_path, "serf", _final_payload(999_999_999))
    ok, reason, _ = lease.claim(tmp_path, "serf", holder="goad", pending=True)
    assert ok and reason == "stolen-stale"


def test_pending_claim_refused_within_grace_then_stolen_after(tmp_path):
    ok, reason, _ = lease.claim(tmp_path, "serf", holder="dispatch", pending=True)
    assert ok
    ok2, reason2, _ = lease.claim(tmp_path, "serf", holder="goad", pending=True)
    assert not ok2 and "spawn in flight" in reason2
    # Age the pending claim past the grace window: the spawner died between
    # claim and Popen — the next spawner must be able to steal it.
    payload = lease.read_lease(tmp_path, "serf")
    payload["started"] -= lease.PENDING_GRACE_SECONDS + 10
    lease.lease_path(tmp_path, "serf").write_text(__import__("json").dumps(payload), encoding="utf-8")
    ok3, reason3, _ = lease.claim(tmp_path, "serf", holder="goad", pending=True)
    assert ok3 and reason3 == "stolen-expired-pending"


def test_claim_force_supersedes_live_holder(tmp_path):
    s = _sleeper()
    try:
        _seed_lease(tmp_path, "serf", _final_payload(s.pid, pgid=os.getpgid(s.pid)))
        ok, reason, _ = lease.claim(tmp_path, "serf", holder="goad", pending=True, force=True)
        assert ok and reason == "force-superseded"
        assert _wait_gone(s.pid)
    finally:
        if lease._pid_alive(s.pid):
            s.kill(); s.wait(timeout=5)


def test_kill_worker_tree_refuses_untrusted_pid():
    """pid-reuse guard: a live pid whose ps command carries no worker token
    is NOT ours — refuse to kill (the /bin/sleep stand-in for a reused pid)."""
    s = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        killed, detail = lease.kill_worker_tree(s.pid, pgid=os.getpgid(s.pid), verify=True)
        assert not killed
        assert "not killing" in detail
        assert lease._pid_alive(s.pid)
    finally:
        s.kill(); s.wait(timeout=5)


def test_kill_worker_tree_kills_group(tmp_path):
    s = _sleeper()
    try:
        killed, detail = lease.kill_worker_tree(s.pid, pgid=os.getpgid(s.pid), verify=False)
        assert killed
        assert _wait_gone(s.pid)
    finally:
        if lease._pid_alive(s.pid):
            s.kill(); s.wait(timeout=5)


# ---------------------------------------------------------------------------
# Log freshness + verdicts + badges
# ---------------------------------------------------------------------------

def test_log_freshness_verdicts(tmp_path):
    (tmp_path / ".kilo").mkdir()
    log = tmp_path / ".kilo" / "serf.log"
    # no log: unknown — never a kill basis
    assert lease.log_freshness(tmp_path, "serf")["verdict"] == "unknown"
    # fresh mtime: live
    log.write_text("tool output", encoding="utf-8")
    assert lease.log_freshness(tmp_path, "serf")["verdict"] == "live"
    # 30m silence: past the serf threshold (20m), within the gatekeeper's (45m)
    old = time.time() - 30 * 60
    os.utime(log, (old, old))
    assert lease.log_freshness(tmp_path, "serf")["verdict"] == "silent"
    gk_log = tmp_path / ".kilo" / "gatekeeper.log"
    gk_log.write_text("suite output", encoding="utf-8")
    os.utime(gk_log, (old, old))
    assert lease.log_freshness(tmp_path, "gatekeeper")["verdict"] == "live"


def test_worker_verdict_states(tmp_path):
    (tmp_path / ".kilo").mkdir()
    log = tmp_path / ".kilo" / "serf.log"
    log.write_text("work", encoding="utf-8")

    assert lease.worker_verdict(tmp_path, "serf")["state"] == "idle"

    s = _sleeper()
    s2 = _sleeper()
    try:
        _seed_lease(tmp_path, "serf", _final_payload(s.pid, pgid=os.getpgid(s.pid)))
        assert lease.worker_verdict(tmp_path, "serf")["state"] == "live"
        old = time.time() - 30 * 60
        os.utime(log, (old, old))
        assert lease.worker_verdict(tmp_path, "serf")["state"] == "stalled"
        os.utime(log, (time.time(), time.time()))
        # dead worker, live watchdog: turn ended, re-prompt owned by watchdog
        _seed_lease(tmp_path, "serf", _final_payload(999_999_999, watchdog_pid=s2.pid, holder="watch"))
        assert lease.worker_verdict(tmp_path, "serf")["state"] == "watching"
        # dead worker, no live watchdog: needs a goad
        _seed_lease(tmp_path, "serf", _final_payload(999_999_999))
        assert lease.worker_verdict(tmp_path, "serf")["state"] == "exited"
    finally:
        for p in (s, s2):
            p.kill(); p.wait(timeout=5)


def test_format_badge_states():
    assert lease.format_badge({"state": "idle"}) == ""
    stalled = lease.format_badge({"state": "stalled", "pid": 7, "log": {"age_seconds": 47 * 60, "exists": True}})
    assert "STALLED" in stalled and "47m" in stalled
    watching = lease.format_badge({"state": "watching", "log": {"age_seconds": None, "exists": False}})
    assert "watchdog" in watching
    exited = lease.format_badge({"state": "exited", "pid": 7, "log": {"age_seconds": None, "exists": False}})
    assert "exited" in exited and "goad" in exited


def test_worker_verdict_for_quest_roles(tmp_path):
    q = MagicMock(kind="", section="Bug fix")
    assert cli._worker_verdict_for_quest(q, None)["state"] == "idle"
    q2 = MagicMock(kind="scout", section="Investigation")
    v = cli._worker_verdict_for_quest(q2, str(tmp_path))
    assert v["role"] == "scout" and v["state"] == "idle"


def test_watch_dir_self_ignored(tmp_path):
    """Engine watch state must never pollute `git status` dirty counts
    (the Q799 false-alarm: .kilo/watch/serf.json flagged as a dirty file)."""
    lease.claim(tmp_path, "serf", holder="dispatch", pending=True)
    gi = tmp_path / ".kilo" / "watch" / ".gitignore"
    assert gi.exists() and gi.read_text().strip() == "*"


# ---------------------------------------------------------------------------
# Spawn-path refusals
# ---------------------------------------------------------------------------

def test_standup_refuses_on_live_lease(tmp_path, monkeypatch):
    # NOTE: create real worker stand-ins BEFORE patching subprocess.Popen —
    # cli.subprocess.Popen IS the global subprocess module, so patching it
    # also mocks any Popen created afterwards.
    s = _sleeper()
    try:
        monkeypatch.delenv("KILO_PORT", raising=False)
        monkeypatch.setattr(cli, "query_kilo_session_ids", lambda p: set())
        spawned = []
        monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: spawned.append(a) or MagicMock(pid=1))
        _seed_lease(tmp_path, "serf", _final_payload(s.pid, pgid=os.getpgid(s.pid)))
        res = cli.standup_kilo_session(
            tmp_path, "serf", "model-x", "do work", "title",
            kilo_bin=Path("/fake/kilo"), run_now=True,
        )
        assert res["ok"] is False and res["mode"] == "lease-held"
        assert str(s.pid) in res["message"]
        assert not _kilo_spawns(spawned), "lease refusal must happen BEFORE any worker Popen"
    finally:
        s.kill(); s.wait(timeout=5)


def test_standup_dead_spawn_releases_lease(tmp_path, monkeypatch):
    monkeypatch.delenv("KILO_PORT", raising=False)
    monkeypatch.setattr(cli, "query_kilo_session_ids", lambda p: set())
    monkeypatch.setattr(cli, "query_latest_kilo_session_id", lambda *a, **k: None)

    class FakeDead:
        pid = 555

        def poll(self):
            return 1

    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: FakeDead())
    res = cli.standup_kilo_session(
        tmp_path, "serf", "model-x", "do work", "title",
        kilo_bin=Path("/fake/kilo"), run_now=True,
    )
    assert res["ok"] is False and res["mode"] == "dead-spawn"
    assert lease.read_lease(tmp_path, "serf") is None


def test_goad_refuses_on_live_lease(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    wt.mkdir()
    s = _sleeper()
    try:
        quest = MagicMock(id="Q799-X", worktree=str(wt), branch="quest/q799-x", kind="", section="Bug fix", serf_model="m")
        monkeypatch.setattr(cli.store, "load", lambda qid: quest)
        monkeypatch.setattr(cli, "find_kilo_binary", lambda: Path("/fake/kilo"))
        monkeypatch.setattr(cli, "query_kilo_session_ids", lambda p: set())
        monkeypatch.setattr(cli.store, "save", lambda *a, **k: None)
        spawned = []
        monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: spawned.append(a) or MagicMock(pid=1))
        _seed_lease(wt, "serf", _final_payload(s.pid, pgid=os.getpgid(s.pid)))
        with pytest.raises(SystemExit):
            cli.cmd_goad(argparse.Namespace(quest_id="Q799-X", model=None, no_commit=True, force=False))
        assert not _kilo_spawns(spawned)
    finally:
        s.kill(); s.wait(timeout=5)


def test_goad_force_supersedes_live_holder(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    wt.mkdir()
    s = _sleeper()
    try:
        quest = MagicMock(id="Q799-X", worktree=str(wt), branch="quest/q799-x", kind="", section="Bug fix", serf_model="m")
        monkeypatch.setattr(cli.store, "load", lambda qid: quest)
        monkeypatch.setattr(cli, "find_kilo_binary", lambda: Path("/fake/kilo"))
        monkeypatch.setattr(cli, "query_kilo_session_ids", lambda p: set())
        monkeypatch.setattr(cli, "query_latest_kilo_session_id", lambda *a, **k: "ses_g")
        monkeypatch.setattr(cli.store, "save", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_spawn_watchdog", lambda *a, **k: None)
        spawned = []
        monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: spawned.append(a) or MagicMock(pid=424242))
        _seed_lease(wt, "serf", _final_payload(s.pid, pgid=os.getpgid(s.pid)))
        cli.cmd_goad(argparse.Namespace(quest_id="Q799-X", model=None, no_commit=True, force=True))
        assert len(_kilo_spawns(spawned)) == 1
        assert _wait_gone(s.pid), "force must kill the superseded holder"
        payload = lease.read_lease(wt, "serf")
        assert payload["pid"] == 424242 and payload["session_id"] == "ses_g"
    finally:
        if lease._pid_alive(s.pid):
            s.kill(); s.wait(timeout=5)


def test_coin_background_refuses_on_live_lease(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    wt.mkdir()
    s = _sleeper()
    try:
        quest = MagicMock(id="Q1", worktree=str(wt), status="TRIBUTE_READY", branch="quest/x",
                          title="t", parent_epic="", master_of_coin_model="")
        monkeypatch.setattr(cli.store, "load", lambda qid: quest)
        monkeypatch.setattr(cli, "find_kilo_binary", lambda: Path("/fake/kilo"))
        monkeypatch.setattr(cli, "query_kilo_session_ids", lambda p: set())
        spawned = []
        monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: spawned.append(a) or MagicMock(pid=1))
        _seed_lease(wt, "master_of_coin", _final_payload(s.pid, pgid=os.getpgid(s.pid), holder="coin"))
        with pytest.raises(SystemExit):
            cli.cmd_coin(argparse.Namespace(quest_id="Q1", model=None, force=False, wait=False, no_commit=True))
        assert not _kilo_spawns(spawned)
    finally:
        s.kill(); s.wait(timeout=5)


# ---------------------------------------------------------------------------
# Watchdog v2: stall referee + duplicate suppression + lease lifecycle
# ---------------------------------------------------------------------------

def test_watch_stall_referee_kills_hung_worker_then_reprompts(tmp_path, monkeypatch):
    """Worker alive but stdout log silent past the threshold: the referee
    kills it (ps-verified foreign pid) and re-prompts the SAME session."""
    s = _sleeper()
    try:
        (tmp_path / ".kilo").mkdir()
        log = tmp_path / ".kilo" / "serf.log"
        log.write_text("last output long ago", encoding="utf-8")
        old = time.time() - 60 * 60
        os.utime(log, (old, old))
        fake_quest = MagicMock(status="WORKING", id="Q1")
        monkeypatch.setattr(cli, "_load_watch_quests", lambda qids, wt: [fake_quest])
        monkeypatch.setattr(cli, "find_kilo_binary", lambda: Path("/fake/kilo"))
        monkeypatch.setattr(cli, "query_latest_kilo_session_id", lambda *a, **k: "")
        monkeypatch.setattr(cli, "_spawn_watchdog", lambda *a, **k: None)
        spawned = []

        class FakeProc:
            pid = 424242

        monkeypatch.setattr(cli.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd) or FakeProc())
        _seed_lease(tmp_path, "serf", _final_payload(s.pid, pgid=os.getpgid(s.pid)))
        monkeypatch.setattr(cli, "WATCH_POLL_SECONDS", 0.01)
        cli.cmd_watch(_make_watch_args(
            dir=str(tmp_path), pid=s.pid, pgid=os.getpgid(s.pid),
            session="ses_w", stall_seconds=1, wall_cap=60, max_attempts=2,
        ))
        assert _wait_gone(s.pid), "stall referee must kill the hung worker"
        assert len(_kilo_spawns(spawned)) == 1
        cmd = _kilo_spawns(spawned)[0]
        assert "--session" in cmd and "ses_w" in cmd
        payload = lease.read_lease(tmp_path, "serf")
        assert payload["pid"] == 424242  # lease moved to the continuation
        assert "stall referee" in log.read_text(encoding="utf-8")
    finally:
        if lease._pid_alive(s.pid):
            s.kill(); s.wait(timeout=5)


def test_watch_suppresses_duplicate_continuation_when_lease_live(tmp_path, monkeypatch):
    """A live foreign worker holds the lease: the watchdog must NOT stack a
    duplicate continuation on top of it (the Q799 stack class)."""
    s = _sleeper()
    try:
        fake_quest = MagicMock(status="WORKING", id="Q1")
        monkeypatch.setattr(cli, "_load_watch_quests", lambda qids, wt: [fake_quest])
        monkeypatch.setattr(cli, "find_kilo_binary", lambda: Path("/fake/kilo"))
        monkeypatch.setattr(cli, "query_latest_kilo_session_id", lambda *a, **k: "")
        spawned = []
        monkeypatch.setattr(cli.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd) or MagicMock(pid=1))
        _seed_lease(tmp_path, "serf", _final_payload(s.pid, pgid=os.getpgid(s.pid), holder="goad"))
        _run_watch_bounded(_make_watch_args(dir=str(tmp_path), wall_cap=5), monkeypatch)
        assert not _kilo_spawns(spawned)
        log_text = (tmp_path / ".kilo" / "serf.log").read_text(encoding="utf-8")
        assert "not spawning a duplicate continuation" in log_text
        assert lease._pid_alive(s.pid)
    finally:
        s.kill(); s.wait(timeout=5)


def test_watch_adopts_lease_and_releases_on_completion(tmp_path, monkeypatch):
    """Manual `court watch` adopts the worker's lease (records watchdog pid);
    when the quest has advanced past the role's active state the lease is
    released, not left dangling."""
    s = _sleeper(seconds=5)
    try:
        fake_quest = MagicMock(status="DONE", id="Q1")
        monkeypatch.setattr(cli, "_load_watch_quests", lambda qids, wt: [fake_quest])
        # The worker pid is foreign to the watchdog; a reaped-dead foreign pid
        # is what _worker_running observes as ended. Simulate the ended turn.
        monkeypatch.setattr(cli, "_worker_running", lambda pid, owned: False)
        _run_watch_bounded(_make_watch_args(dir=str(tmp_path), pid=s.pid, wall_cap=60), monkeypatch)
        payload = lease.read_lease(tmp_path, "serf")
        # released on clean completion
        assert payload is None
    finally:
        if lease._pid_alive(s.pid):
            s.kill(); s.wait(timeout=5)


def test_watch_wall_cap_abandon_keeps_live_workers_lease(tmp_path, monkeypatch):
    """Abandoning at the wall cap while the worker is still alive must keep
    the lease (the worker genuinely runs; the dashboard should say live)."""
    s = _sleeper(seconds=30)
    try:
        fake_quest = MagicMock(status="WORKING", id="Q1")
        monkeypatch.setattr(cli, "_load_watch_quests", lambda qids, wt: [fake_quest])
        _run_watch_bounded(_make_watch_args(dir=str(tmp_path), pid=s.pid, wall_cap=1), monkeypatch)
        assert lease._pid_alive(s.pid)
        payload = lease.read_lease(tmp_path, "serf")
        assert payload is not None and payload["pid"] == s.pid
        assert payload.get("watchdog_pid")
    finally:
        s.kill(); s.wait(timeout=5)
        lease.release(tmp_path, "serf")
