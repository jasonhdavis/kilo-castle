"""Tests for the royal decree lane (`court.decisions`)."""
import json
import subprocess
from pathlib import Path

import pytest

from court import decisions
from court import nightwatch


@pytest.fixture(autouse=True)
def _state_outside_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(nightwatch, "state_dir", lambda repo: tmp_path / "state")


def _git(repo, *argv):
    return subprocess.run(["git", "-C", str(repo), *argv], check=True,
                          capture_output=True, text=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "castle")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test Author")
    (repo / ".court" / "quests").mkdir(parents=True)
    (repo / ".court" / "LEDGER.md").write_text("- realm founded\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def _quest_text(qid="Q123-Held-Thing", status="HELD", audience="None required.",
                aud_log=""):
    return f"""---
id: {qid}
title: The held thing
kind: quest
status: {status}
branch: quest/{qid.split('-')[0].lower()}-held-thing
---

# {qid} — The held thing

## Castle Ledger
- **2026-11-01T00:00:00Z** — WORKING → HELD: waiting on auth model

## The Kingdom Requires
Deliver it.

## Expected Tribute
- [ ] it works

## Tribute Rendered
### Audience
{audience}

## Audience Log
{aud_log}
"""


def _add_quest(repo, **kwargs):
    text = _quest_text(**kwargs)
    qid = text.split("id: ", 1)[1].split("\n", 1)[0]
    (repo / ".court" / "quests" / f"{qid}.md").write_text(text, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"charter {qid}")
    return qid


def _packet(qid, ruling="Use session cookies.", status="WORKING", **extra):
    payload = {
        "id": extra.pop("id", "decree-2026-11-05-1200"),
        "by": extra.pop("by", "M'Lord"),
        "source": extra.pop("source", "voice via the Pleasance agent"),
        "decisions": [{"quest": qid, "ruling": ruling, **extra}],
    }
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------


def test_parses_a_batch_packet():
    text = json.dumps({
        "id": "decree-1",
        "by": "M'Lord",
        "at": "2026-11-05T12:00:00Z",
        "decisions": [
            {"quest": "Q1-A", "ruling": "Yes", "status": "working"},
            {"quest": "Q2-B", "ruling": "Nope", "question": "Ship it?"},
        ],
    })
    packet = decisions.parse_decree_packet(text)
    assert packet["id"] == "decree-1"
    assert packet["decisions"][0]["status"] == "WORKING"
    assert packet["decisions"][1]["status"] == ""


@pytest.mark.parametrize("payload,needle", [
    ({"id": "x", "by": "M'Lord", "decisions": [{"quest": "Q1", "ruling": "yes"}]},
     "id must be"),
    ({"id": "decree-1", "by": "", "decisions": [{"quest": "Q1", "ruling": "yes"}]},
     "who ruled"),
    ({"id": "decree-1", "by": "M'Lord", "decisions": []}, "at least one decision"),
    ({"id": "decree-1", "by": "M'Lord",
      "decisions": [{"quest": "Q1", "ruling": ""}]}, "needs a `ruling`"),
    ({"id": "decree-1", "by": "M'Lord",
      "decisions": [{"quest": "Q1", "ruling": "yes", "status": "READY_TO_RAZE"}]},
     "razing/shipping/pillory stay human acts"),
    ({"id": "decree-1", "by": "M'Lord",
      "decisions": [{"quest": "Q1", "ruling": "yes", "status": "PUNISHED"}]},
     "decrees may only target"),
])
def test_rejects_malformed_packets(payload, needle):
    with pytest.raises(decisions.DecreeError) as exc:
        decisions.parse_decree_packet(json.dumps(payload))
    assert needle in str(exc.value)


def test_validate_requires_the_quest_to_exist_on_castle(tmp_path):
    repo = _repo(tmp_path)
    _add_quest(repo)
    ok, why = decisions.validate_decree(_packet("Q123-Held-Thing"), repo)
    assert ok, why
    ok, why = decisions.validate_decree(_packet("Q999-Ghost"), repo)
    assert not ok and "unknown quest" in why


def test_a_declared_directive_needs_no_charter(tmp_path):
    """A ruling about the pipeline ("muster a Steward for merge triage") names
    no Quest. Declared as a directive it lands in `.court/directives/` instead
    of being rejected on every pickup tick."""
    repo = _repo(tmp_path)
    text = _packet("merge-blocker-reconciliation", directive=True)
    ok, why = decisions.validate_decree(text, repo)
    assert ok, why

    packet = decisions.parse_decree_packet(text)
    result = decisions.apply_packets(repo, [packet])
    written = repo / ".court" / "directives" / "merge-blocker-reconciliation.md"
    assert written.exists()
    assert "Use session cookies." in written.read_text(encoding="utf-8")
    assert result["applied"][0]["to"] == "DIRECTIVE"


def test_an_unknown_q_id_is_still_rejected_even_if_declared_a_directive(tmp_path):
    """Directive is not a bypass for a typo'd Quest id."""
    repo = _repo(tmp_path)
    ok, why = decisions.validate_decree(_packet("Q999-Ghost", directive=True), repo)
    assert not ok and "unknown quest" in why


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def test_applies_ruling_to_the_audience_log_and_unblocks(tmp_path):
    repo = _repo(tmp_path)
    _add_quest(repo)
    packet = decisions.parse_decree_packet(_packet("Q123-Held-Thing"))
    result = decisions.apply_packets(repo, [packet])

    assert result["decree_ids"] == ["decree-2026-11-05-1200"]
    assert result["applied"][0]["from"] == "HELD"
    assert result["applied"][0]["to"] == "WORKING"

    from court import store
    quest = store.load("Q123-Held-Thing", court_root=repo / ".court")
    assert quest.status == "WORKING"
    assert quest.has_pending_audience() is False
    log = quest.body_sections["Audience Log"]
    assert "Royal Decree `decree-2026-11-05-1200`" in log
    assert "Use session cookies." in log
    assert "Resulting status:** WORKING" in log


def test_implicit_status_resumes_a_held_quest_but_leaves_working_alone(tmp_path):
    repo = _repo(tmp_path)
    _add_quest(repo)
    packet = decisions.parse_decree_packet(_packet("Q123-Held-Thing", status=""))
    decisions.apply_packets(repo, [packet])

    from court import store
    assert store.load("Q123-Held-Thing", court_root=repo / ".court").status == "WORKING"

    _add_quest(repo, qid="Q124-Working-Thing", status="WORKING", audience="")
    packet = decisions.parse_decree_packet(_packet("Q124-Working-Thing", status=""))
    decisions.apply_packets(repo, [packet])
    quest = store.load("Q124-Working-Thing", court_root=repo / ".court")
    assert quest.status == "WORKING"
    assert "Use session cookies." in quest.body_sections["Audience Log"]


def test_one_packet_can_clear_several_quests(tmp_path):
    repo = _repo(tmp_path)
    _add_quest(repo, qid="Q123-Held-Thing")
    _add_quest(repo, qid="Q124-Other-Thing")
    text = json.dumps({
        "id": "decree-batch",
        "by": "M'Lord",
        "decisions": [
            {"quest": "Q123-Held-Thing", "ruling": "Go with cookies.", "status": "WORKING"},
            {"quest": "Q124-Other-Thing", "ruling": "Go with tokens.", "status": "PLANNED"},
        ],
    })
    result = decisions.apply_packets(repo, [decisions.parse_decree_packet(text)])
    assert [row["quest"] for row in result["applied"]] == ["Q123-Held-Thing", "Q124-Other-Thing"]

    from court import store
    assert store.load("Q123-Held-Thing", court_root=repo / ".court").status == "WORKING"
    assert store.load("Q124-Other-Thing", court_root=repo / ".court").status == "PLANNED"


# ---------------------------------------------------------------------------
# The frontier brief
# ---------------------------------------------------------------------------


def test_collect_lists_held_and_pending_audience_quests(tmp_path):
    repo = _repo(tmp_path)
    _add_quest(repo, qid="Q123-Held-Thing", status="HELD")
    _add_quest(repo, qid="Q124-Audience-Thing", status="TRIBUTE_READY",
               audience="Should the export be CSV or XLSX? Pick one before shipping.")

    items = decisions.collect(repo)
    by_id = {item["id"]: item for item in items}
    assert set(by_id) == {"Q123-Held-Thing", "Q124-Audience-Thing"}
    assert by_id["Q123-Held-Thing"]["kind"] == "blocked"
    assert by_id["Q124-Audience-Thing"]["kind"] == "audience"
    assert "CSV or XLSX" in by_id["Q124-Audience-Thing"]["question"]
    assert "Audience Log" in by_id["Q124-Audience-Thing"]["next"]


def test_collect_ignores_terminal_and_unblocked_quests(tmp_path):
    repo = _repo(tmp_path)
    _add_quest(repo, qid="Q123-Held-Thing", status="WORKING", audience="None required.")
    _add_quest(repo, qid="Q124-Done-Thing", status="READY_TO_RAZE", audience="")
    assert decisions.collect(repo) == []


def test_render_markdown_carries_a_ready_to_use_skeleton(tmp_path):
    repo = _repo(tmp_path)
    _add_quest(repo, qid="Q123-Held-Thing")
    md = decisions.render_markdown(decisions.collect(repo), "kilo-castle")
    assert "Decisions awaiting M'Lord — kilo-castle" in md
    assert '"quest": "Q123-Held-Thing"' in md
    assert '"status": "WORKING"' in md
    assert "`pleasance` branch" in md


def test_cmd_decree_authors_and_applies_a_local_ruling(tmp_path):
    from argparse import Namespace

    repo = _repo(tmp_path)
    _add_quest(repo)
    args = Namespace(quest="Q123-Held-Thing", ruling="Use session cookies.",
                     question="Which auth model?", status="WORKING",
                     id="decree-local", by="M'Lord", repo=str(repo), no_commit=False)
    decisions.cmd_decree(args)

    packet_path = repo / ".court" / "decrees" / "decree-local.json"
    assert packet_path.exists()
    assert "decree-local" in _git(repo, "log", "-1", "--format=%s").stdout

    from court import store
    quest = store.load("Q123-Held-Thing", court_root=repo / ".court")
    assert quest.status == "WORKING"
    assert "Use session cookies." in quest.body_sections["Audience Log"]
