"""Tests for the compact steward pulse (`court.pulse`)."""
import json
import subprocess
import time
from pathlib import Path

import pytest

from court import nightwatch
from court import pulse


def _git(repo, *argv):
    return subprocess.run(["git", "-C", str(repo), *argv], check=True,
                          capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _state_outside_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(nightwatch, "state_dir", lambda repo: tmp_path / "state")


def _repo(tmp_path, branch="castle"):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", branch)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test Author")
    (repo / ".court").mkdir()
    (repo / ".court" / "LEDGER.md").write_text(
        "- **2026-11-04T10:00:00Z** — realm founded\n"
        "- **2026-11-05T03:20:00Z** — pleasance pickup applied abc123: 1 charter(s)\n",
        encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def _snap():
    return {
        "quests": [
            {"id": "Q100-Alpha", "title": "Alpha", "kind": "quest",
             "status": "TRIBUTE_READY", "pending_audience": False},
            {"id": "Q101-Beta", "title": "Beta", "kind": "quest",
             "status": "GATE", "pending_audience": False},
            {"id": "Q102-Gamma", "title": "Gamma", "kind": "quest",
             "status": "WORKING", "pending_audience": True},
            {"id": "Q900-Epic", "title": "Epic", "kind": "epic",
             "status": "WORKING"},
        ]
    }


def test_build_and_render_answers_the_four_questions(tmp_path):
    repo = _repo(tmp_path)
    now = time.time()
    state = {
        "last_tick": "2026-11-05T03:26:24Z",
        "last_tick_epoch": now - 42,
        "policy_line": "enable=goad,coin · workers<=3",
        "convoys": {"cogship-042": {"stale": {"reason": "dead gatehouse"}}},
        "last_pickup": {"ts": "2026-11-05T03:20:00Z", "picked": [], "rejected": "bad path"},
    }
    journal = [
        {"ts": "2026-11-05T03:20:00Z", "event": "tick",
         "statuses": {"Q100-Alpha": "WORKING", "Q102-Gamma": "WORKING"}},
        {"ts": "2026-11-05T03:22:00Z", "event": "tick",
         "statuses": {"Q100-Alpha": "TRIBUTE_READY", "Q102-Gamma": "WORKING"}},
        {"ts": "2026-11-05T03:23:00Z", "event": "action",
         "action": {"kind": "coin", "quest_ids": ["Q100-Alpha"], "reason": "tribute done"},
         "result": {"ok": True}},
    ]
    payload = pulse.build(
        repo, snap=_snap(),
        workers={"count": 2, "rss_mb": 640.5, "by_role": {"serf": 2}},
        state=state, journal=journal, notes=[], limit=15)

    assert payload["running"]["watch"]["state"] == "cron"
    assert payload["running"]["watch"]["last_tick_age_seconds"] >= 42
    assert payload["running"]["board"]["total"] == 3
    assert any("Q100: WORKING → TRIBUTE_READY" in e["text"] for e in payload["events"])
    assert any("coin Q100" in e["text"] for e in payload["events"])
    assert [q["id"] for q in payload["queued"]["tribute_ready"]] == ["Q100-Alpha"]
    assert [q["id"] for q in payload["queued"]["gate"]] == ["Q101-Beta"]
    assert any("pending Serf Audience" in a["text"] for a in payload["attention"])
    assert any("stale gatehouse" in a["text"] for a in payload["attention"])
    assert any("last pickup rejected" in a["text"] for a in payload["attention"])

    md = pulse.render_markdown(payload)
    assert "Is the machine running?" in md
    assert "What landed" in md
    assert "Queued / in flight" in md
    assert "Needs attention" in md
    assert "Sending a command" in md
    assert "status/pleasance" in md


def test_write_local_lands_outside_the_repo(tmp_path):
    repo = _repo(tmp_path)
    md_path, js_path = pulse.write_local(repo, "# pulse\n", "{}\n")
    assert md_path.is_file() and js_path.is_file()
    assert repo not in md_path.parents


def test_publish_creates_and_then_reuses_status_branch(tmp_path):
    repo = _repo(tmp_path)
    bare = tmp_path / "origin.git"
    _git(repo, "init", "--bare", str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-u", "origin", "castle")

    first = pulse.publish(repo, "# Pulse v1\n", '{"v":1}\n')
    assert first["published"] is True
    assert _git(repo, "show", "origin/status/pleasance:PULSE.md").stdout == "# Pulse v1\n"
    assert json.loads(_git(repo, "show", "origin/status/pleasance:PULSE.json").stdout) == {"v": 1}

    second = pulse.publish(repo, "# Pulse v1\n", '{"v":1}\n')
    assert second["published"] is False
    assert second["reason"] == "unchanged"

    third = pulse.publish(repo, "# Pulse v2\n", '{"v":2}\n')
    assert third["published"] is True
    assert _git(repo, "show", "origin/status/pleasance:PULSE.md").stdout == "# Pulse v2\n"
    # Disposable mirror: one commit, no growing heartbeat history.
    count = _git(repo, "rev-list", "--count", "refs/heads/status/pleasance").stdout.strip()
    assert count == "1"


def test_publish_does_not_disturb_the_working_index(tmp_path):
    repo = _repo(tmp_path)
    bare = tmp_path / "origin.git"
    _git(repo, "init", "--bare", str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-u", "origin", "castle")
    staged = repo / "staged.txt"
    staged.write_text("keep me staged\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")

    assert pulse.publish(repo, "# Pulse\n", "{}\n")["published"] is True
    assert "staged.txt" in _git(repo, "diff", "--cached", "--name-only").stdout


def test_pickup_is_refused_off_the_castle_checkout(tmp_path):
    repo = _repo(tmp_path, branch="main")
    state = {}
    result = nightwatch._run_pickup(repo, state, dry_run=True, say=lambda *a, **k: None)
    assert "not on castle" in result["skipped"]
    assert "not on castle" in state["last_pickup"]["skipped"]


def test_pulse_surfaces_the_decisions_awaiting_mlord(tmp_path):
    repo = _repo(tmp_path)
    payload = pulse.build(
        repo, snap=_snap(), workers={"count": 0, "rss_mb": 0.0, "by_role": {}},
        state={}, journal=[], notes=[], limit=15)

    assert [item["id"] for item in payload["decisions"]] == ["Q102-Gamma"]
    assert payload["decisions"][0]["kind"] == "audience"

    md = pulse.render_markdown(payload)
    assert "## Decisions awaiting M'Lord" in md
    assert "Q102" in md
    assert ".court/decrees/<id>.json" in md
