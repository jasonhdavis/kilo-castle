"""Dashboard ledger auto-reconcile (`court.ui_server`): the manifest gate
(`config.get_auto_sync`) and the classified `court sync --all` sweep that
folds branch-tip paperwork into each checkout on a cadence. Pure helpers —
subprocess is mocked, no real ledger is written."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

from court import config as court_config
from court import ui_server as u


def _write_config(tmp_path, sync=None):
    court_dir = tmp_path / ".court"
    court_dir.mkdir(parents=True, exist_ok=True)
    cfg = {} if sync is None else {"sync": sync}
    (court_dir / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return court_dir


def test_auto_sync_absent_is_disabled(tmp_path):
    court_dir = _write_config(tmp_path, sync=None)
    got = court_config.get_auto_sync(court_dir)
    assert got == {"enabled": False, "interval_minutes": 0.0}


def test_auto_sync_zero_is_disabled(tmp_path):
    court_dir = _write_config(tmp_path, sync={"auto_interval_minutes": 0})
    assert court_config.get_auto_sync(court_dir)["enabled"] is False


def test_auto_sync_positive_interval_enables(tmp_path):
    court_dir = _write_config(tmp_path, sync={"auto_interval_minutes": 20})
    got = court_config.get_auto_sync(court_dir)
    assert got == {"enabled": True, "interval_minutes": 20.0}


def test_auto_sync_garbage_interval_is_disabled(tmp_path):
    court_dir = _write_config(tmp_path, sync={"auto_interval_minutes": "soon"})
    assert court_config.get_auto_sync(court_dir)["enabled"] is False


def _fake_run(returncode=0, stdout="Ledger sync: 3 synced, 0 united", stderr=""):
    calls = []

    def run(argv, capture_output=True, text=True, timeout=None, cwd=None):
        calls.append({"argv": argv, "cwd": cwd, "timeout": timeout})
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    run.calls = calls
    return run


def test_auto_sync_cycle_sweeps_every_ledger_root(monkeypatch):
    monkeypatch.setattr(u, "_ledger_repo_roots", lambda: ["/r/a", "/r/b"])
    fake = _fake_run()
    monkeypatch.setattr(u.subprocess, "run", fake)

    results = u._auto_sync_cycle()

    assert [r["root"] for r in results] == ["/r/a", "/r/b"]
    assert all(r["ok"] for r in results)
    assert "3 synced" in results[0]["summary"]
    # the classified global fold, run once per checkout
    assert fake.calls[0]["argv"][-2:] == ["sync", "--all"]
    assert [c["cwd"] for c in fake.calls] == ["/r/a", "/r/b"]


def test_auto_sync_cycle_reports_failure_without_raising(monkeypatch):
    monkeypatch.setattr(u, "_ledger_repo_roots", lambda: ["/r/a"])
    monkeypatch.setattr(u.subprocess, "run",
                        _fake_run(returncode=1, stdout="", stderr="boom"))
    results = u._auto_sync_cycle()
    assert results[0]["ok"] is False and "exit 1" in results[0]["summary"]


def test_auto_sync_cycle_clears_state_caches(monkeypatch):
    monkeypatch.setattr(u, "_ledger_repo_roots", lambda: [])
    monkeypatch.setattr(u.subprocess, "run", _fake_run())
    u._QUESTS_CACHE["/r/a"] = (1.0, [])
    u._WT_CACHE["/r/a"] = (1.0, [])
    u._DIRTY_CACHE["/r/a/wt"] = (1.0, True)
    u._BR_CACHE["/r/a"] = (1.0, [])
    u._AUDIT_CACHE["/r/a"] = {"t": 1.0, "data": {}}

    u._auto_sync_cycle()

    for cache in (u._QUESTS_CACHE, u._WT_CACHE, u._DIRTY_CACHE, u._BR_CACHE,
                  u._AUDIT_CACHE):
        assert cache == {}


def test_auto_sync_status_reflects_manifest(tmp_path, monkeypatch):
    court_dir = _write_config(tmp_path, sync={"auto_interval_minutes": 30})
    monkeypatch.setattr(u, "COURT_DIR", str(tmp_path))
    monkeypatch.setitem(u.AUTO_SYNC, "last_run", time.time() - 120)
    monkeypatch.setitem(u.AUTO_SYNC, "results",
                        [{"root": str(tmp_path), "ok": True, "summary": "ok"}])

    status = u._auto_sync_status()

    assert status["enabled"] is True
    assert status["interval_minutes"] == 30.0
    assert status["last_run_ago"].endswith("m")
    assert status["results"][0]["ok"] is True


def test_start_auto_sync_is_idempotent(monkeypatch):
    started = []
    monkeypatch.setitem(u.AUTO_SYNC, "thread", None)
    monkeypatch.setattr(
        u.threading, "Thread",
        lambda *a, **k: started.append(k) or SimpleNamespace(start=lambda: None))

    first = u.start_auto_sync()
    second = u.start_auto_sync()

    assert first is second
    assert len(started) == 1
    assert started[0].get("daemon") is True
    monkeypatch.setitem(u.AUTO_SYNC, "thread", None)
