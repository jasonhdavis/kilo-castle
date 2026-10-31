import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from court import metrics
from court import store
from court.cli import main
from court.models import Quest


def _make_kilo_db(tmp_path, sessions, messages):
    """Build a minimal stand-in for the real kilo.db session/message schema."""
    db = tmp_path / "kilo.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE session ("
        "id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, agent TEXT, "
        "model TEXT, title TEXT, time_created INTEGER, time_updated INTEGER, "
        "cost REAL, tokens_input INTEGER, tokens_output INTEGER, "
        "tokens_reasoning INTEGER, tokens_cache_read INTEGER, "
        "tokens_cache_write INTEGER)"
    )
    conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT)")
    for s in sessions:
        conn.execute(
            "INSERT INTO session (id, parent_id, directory, agent, model, title, "
            "time_created, time_updated, cost, tokens_input, tokens_output, "
            "tokens_reasoning, tokens_cache_read, tokens_cache_write) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                s["id"], s.get("parent_id", ""), s.get("directory", ""),
                s.get("agent", "code"), s.get("model", ""), s.get("title", ""),
                s.get("time_created", 0), s.get("time_updated", 0),
                s.get("cost", 0.0), s.get("tokens_input", 0), s.get("tokens_output", 0),
                s.get("tokens_reasoning", 0), s.get("tokens_cache_read", 0),
                s.get("tokens_cache_write", 0),
            ),
        )
    for i, m in enumerate(messages):
        conn.execute(
            "INSERT INTO message (id, session_id, data) VALUES (?,?,?)",
            (f"m{i}", m["session_id"], json.dumps(m["data"])),
        )
    conn.commit()
    conn.close()
    return db


def _model_json(mid, provider="openrouter"):
    return json.dumps({"id": mid, "providerID": provider, "variant": "default"})


def test_quest_metrics_binds_worktree_sessions_and_children(tmp_path):
    wt = tmp_path / "wt-quest"
    (wt / ".kilo").mkdir(parents=True)
    (wt / ".kilo" / "serf.log").write_text(
        "--- Goad Serf: Q001 Serf Goad (2026-10-01T00:00:00) ---\n"
        "--- Goad Serf: Q001 Serf Goad (2026-10-01T01:00:00) ---\n"
    )
    db = _make_kilo_db(
        tmp_path,
        sessions=[
            {
                "id": "s1", "directory": str(wt), "agent": "serf",
                "model": _model_json("z-ai/glm-5.3-flash"), "title": "Q001 Serf Worker",
                "time_created": 1_000_000, "time_updated": 1_060_000, "cost": 0.25,
                "tokens_input": 100, "tokens_output": 50, "tokens_reasoning": 10,
            },
            {
                "id": "s2", "parent_id": "s1", "directory": str(tmp_path / "subagent-wt"),
                "agent": "serf",
                "model": _model_json("z-ai/glm-5.3-flash"), "title": "Q001 Serf Worker",
                "time_created": 1_030_000, "time_updated": 1_120_000, "cost": 0.10,
            },
            {
                "id": "s3", "directory": str(tmp_path / "other-wt"), "agent": "serf",
                "model": _model_json("other/model"), "title": "Q999 Serf Worker",
                "time_created": 1_000_000, "time_updated": 9_000_000, "cost": 99.0,
            },
        ],
        messages=[
            {"session_id": "s1", "data": {"role": "user"}},
            {"session_id": "s1", "data": {"role": "assistant"}},
            {"session_id": "s1", "data": {"role": "assistant"}},
            {"session_id": "s2", "data": {"role": "user"}},
            {"session_id": "s2", "data": {"role": "assistant"}},
            {"session_id": "s3", "data": {"role": "user"}},
        ],
    )

    q = Quest(id="Q001-Demo", title="Demo", app="core", concern="demo", worktree=str(wt))
    rollup = metrics.rollup_metrics([q], court_root=tmp_path / ".court", db_path=db)
    assert rollup["db_available"] is True
    m = rollup["quests"][0]

    # Only the worktree-bound sessions (s1 + its child s2) count; s3 is elsewhere.
    assert m["sessions"] == 2
    assert m["cost_usd"] == pytest.approx(0.35)
    assert m["wall_clock_seconds"] == pytest.approx((1_120_000 - 1_000_000) / 1000.0)
    assert m["turns"] == 2  # one user prompt per session
    assert m["assistant_messages"] == 3
    assert m["goads"] == 2  # two log-ledger markers
    assert m["goads_log"] == 2
    assert m["models"] == ["z-ai/glm-5.3-flash"]
    assert m["tokens"]["input"] == 100
    assert m["by_role"]["serf"]["sessions"] == 2


def test_goads_fall_back_to_db_titles_without_logs(tmp_path):
    wt = tmp_path / "wt-b"
    wt.mkdir()
    db = _make_kilo_db(
        tmp_path,
        sessions=[
            {
                "id": "g1", "directory": str(wt), "agent": "serf",
                "title": "Q002-B Serf Goad", "time_created": 0, "time_updated": 1000,
                "cost": 0.0,
            },
        ],
        messages=[],
    )
    q = Quest(id="Q002-B", title="B", app="core", concern="b", worktree=str(wt))
    m = metrics.rollup_metrics([q], court_root=tmp_path / ".court", db_path=db)["quests"][0]
    assert m["goads_log"] == 0
    assert m["goads_db"] == 1
    assert m["goads"] == 1


def test_missing_db_is_still_read_only_and_zeroes(tmp_path):
    wt = tmp_path / "wt-c"
    wt.mkdir()
    q = Quest(id="Q003-C", title="C", app="core", concern="c", worktree=str(wt))
    rollup = metrics.rollup_metrics(
        [q], court_root=tmp_path / ".court", db_path=tmp_path / "nope.db"
    )
    assert rollup["db_available"] is False
    assert rollup["quests"][0]["cost_usd"] == 0.0


def test_readonly_db_file_is_never_written(tmp_path):
    wt = tmp_path / "wt-ro"
    wt.mkdir()
    db = _make_kilo_db(
        tmp_path,
        sessions=[{
            "id": "r1", "directory": str(wt), "agent": "serf",
            "title": "Q004-R Worker", "time_created": 0, "time_updated": 5000,
            "cost": 0.5,
        }],
        messages=[{"session_id": "r1", "data": {"role": "user"}}],
    )
    before = db.read_bytes()
    os.chmod(db, 0o444)  # a write attempt would now fail
    try:
        q = Quest(id="Q004-R", title="R", app="core", concern="r", worktree=str(wt))
        m = metrics.rollup_metrics([q], court_root=tmp_path / ".court", db_path=db)["quests"][0]
        assert m["cost_usd"] == 0.5
        assert db.read_bytes() == before
    finally:
        os.chmod(db, 0o644)


def _init_git_repo(path):
    subprocess.run(["git", "init", "-q", "-b", "castle"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


def test_cli_metrics_json(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setenv("COURT_DIR", str(tmp_path / ".court"))
    monkeypatch.chdir(tmp_path)

    wt = tmp_path / "wt-cli"
    wt.mkdir()
    db = _make_kilo_db(
        tmp_path,
        sessions=[{
            "id": "c1", "directory": str(wt), "agent": "serf",
            "model": _model_json("deepseek/deepseek-v4.1-flash"),
            "title": "Q001-Cli Worker", "time_created": 0, "time_updated": 2000,
            "cost": 0.123456,
        }],
        messages=[
            {"session_id": "c1", "data": {"role": "user"}},
            {"session_id": "c1", "data": {"role": "assistant"}},
        ],
    )
    q = Quest(id="Q001-Cli", title="Cli", app="core", concern="cli", worktree=str(wt))
    store.save(q, court_root=tmp_path / ".court", auto_commit=False)

    monkeypatch.setenv("COURT_KILO_DB", str(db))
    main(["metrics", "Q001-Cli", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["db_available"] is True
    assert payload["totals"]["sessions"] == 1
    assert payload["quests"][0]["cost_usd"] == pytest.approx(0.123456)
    assert payload["quests"][0]["turns"] == 1
