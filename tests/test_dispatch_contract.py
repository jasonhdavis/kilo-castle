"""Tests for the Night's Watch enqueue contract: queue trace, dispatch guard, compliance."""
import json
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

from court import nightwatch as nw
from court import store
from court.cli import _watch_dispatch_block, cmd_enqueue
from court.models import Quest


def _repo(tmp_path):
    (tmp_path / ".court" / "quests").mkdir(parents=True)
    return tmp_path


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _charter(repo, qid, ts_iso):
    path = repo / ".court" / "quests" / f"{qid}-Test.md"
    path.write_text(
        f"---\nid: {qid}\nstatus: WORKING\n---\n\n"
        f"- **{ts_iso}** — PLANNED → DISPATCHED: Serf dispatched (`court dispatch`)\n"
    )
    return path


def _quest(qid, status="PLANNED"):
    return Quest(id=qid, title=qid, kind="quest", app="core", concern="x", status=status)


def test_compliance_classifies_queued_and_direct(tmp_path):
    repo = _repo(tmp_path)
    now = _now()
    _charter(repo, "Q1-Core-A", now)
    _charter(repo, "Q2-Core-B", now)
    vigils_dir = repo / ".court" / "vigils"
    vigils_dir.mkdir(parents=True)
    (vigils_dir / "dispatch-q1.json").write_text(json.dumps({
        "id": "dispatch-q1",
        "then": "dispatch:Q1-Core-A",
        "status": "fired",
        "fires": [{"ts": now, "ok": True}],
    }))

    compliance = nw.dispatch_compliance(repo)
    assert [qid for qid, _ in compliance["queued"]] == ["Q1"]
    assert [qid for qid, _ in compliance["direct"]] == ["Q2"]


def test_the_digest_reports_the_contract(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(nw, "state_dir", lambda r: tmp_path / "state")
    _charter(repo, "Q2-Core-B", _now())
    digest = nw.render_digest(repo, journal=[])
    assert "dispatch contract (24h)" in digest
    assert "Q2 dispatched without a dispatch vigil" in digest


def test_new_work_is_refused_while_the_watch_is_live(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(nw, "state_dir", lambda r: tmp_path / "state")
    (tmp_path / "state").mkdir()

    def _stamp(value):
        state = nw.load_state(repo)
        state["last_tick"] = value
        nw.save_state(repo, state)

    _stamp(_now())

    reason = _watch_dispatch_block(repo, _quest("Q3-Core-C", "PLANNED"))
    assert "Night's Watch is live" in reason and "court enqueue" in reason

    assert _watch_dispatch_block(repo, _quest("Q3-Core-C", "WORKING")) == ""

    _stamp("2020-01-01T00:00:00Z")
    assert _watch_dispatch_block(repo, _quest("Q3-Core-C", "PLANNED")) == ""


def test_enqueue_writes_a_ledger_trace(tmp_path):
    repo = _repo(tmp_path)
    store.save(_quest("Q4-Core-D"), court_root=repo / ".court", auto_commit=False)

    cmd_enqueue(Namespace(quest="Q4-Core-D", repo=str(repo), why="ready to build",
                          expires="24h", after="0s"))

    text = (repo / ".court" / "quests" / "Q4-Core-D.md").read_text()
    assert "Queued for the Night's Watch" in text
