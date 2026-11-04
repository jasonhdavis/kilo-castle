"""Integration tests for the pleasance pickup (court.pickup).

Every test runs against a real temp git repo cloned on the `castle` trunk with
a bare `origin` remote, and pushes authored paperwork up the `pleasance`
branch exactly as an away Steward's voice-chat LLM would — one branch, direct
commits, no synthesized vigil files.
"""
import json
import subprocess
from pathlib import Path

import pytest

from court import pickup
from court import vigils


def _git(repo, *argv):
    return subprocess.run(["git", "-C", str(repo), *argv], check=True,
                          capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _pickup_state_outside_repo(tmp_path, monkeypatch):
    """Keep pickup applied-state (and the nightwatch state dir it reuses)
    inside the tmp sandbox so tests are hermetic."""
    monkeypatch.setattr(pickup.nightwatch, "state_dir", lambda repo: tmp_path / "native-state")


def _repo(tmp_path):
    """A temp Court repo: `castle` trunk with .court/LEDGER.md committed,
    a `main` branch, and a bare `origin` remote."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "castle")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test Author")
    (repo / ".court").mkdir(parents=True)
    (repo / ".court" / "LEDGER.md").write_text(
        "- **2000-01-01T00:00:00Z** — realm founded\n", encoding="utf-8")
    # the court write lock is a tracked, stable 0-byte file in the real repo
    # (court/git_ops.court_write_lock); mirror it so the pickup never sweeps a
    # fresh lock into a stub branch or an untracked-dirty state.
    (repo / ".court" / ".write.lock").write_text("", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init castle")
    _git(repo, "branch", "main")
    bare = tmp_path / "origin.git"
    _git(repo, "init", "--bare", str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-u", "origin", "castle")
    _git(repo, "push", "origin", "main")
    return repo


def _please(repo, builder):
    """Author `builder(repo)` on the pleasance branch and push it to origin."""
    _git(repo, "checkout", "-b", "pleasance", "castle")
    builder(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "pleasance paperwork")
    _git(repo, "push", "-u", "origin", "pleasance")
    _git(repo, "checkout", "castle")


def _please_again(repo, builder):
    """Append a second commit to the existing pleansance branch and push it."""
    _git(repo, "checkout", "pleasance")
    builder(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "pleasance paperwork 2")
    _git(repo, "push", "origin", "pleasance")
    _git(repo, "checkout", "castle")


def _charter(qid, title, status="CHARTERED", branch="quest/q999-new-slug", prereqs=()):
    lines = [
        "---",
        f"id: {qid}",
        f"title: {title}",
        "kind: quest",
        f"status: {status}",
        f"branch: {branch}",
        "---",
        "",
        f"# {qid} — {title}",
        "",
        "## The Kingdom Requires",
        "Deliver the thing.",
        "",
        "## Prerequisites",
    ]
    lines += [f"- {p}" for p in prereqs]
    return "\n".join(lines) + "\n"


def _valid_builder(r):
    (r / ".court" / "quests").mkdir(parents=True, exist_ok=True)
    (r / ".court" / "quests" / "Q999-Pickup-Lane.md").write_text(
        _charter("Q999-Pickup-Lane", "New pickup quest", status="CHARTERED",
                 branch="quest/q999-pickup-lane",
                 prereqs=["quest-status:Q999-Pickup-Lane=CHARTERED"]),
        encoding="utf-8")


def _silent():
    return lambda *a, **k: None


# ---------------------------------------------------------------------------
# Rejections — batch-wide, applied_tip untouched
# ---------------------------------------------------------------------------


def test_rejects_a_path_outside_court(tmp_path):
    repo = _repo(tmp_path)
    _please(repo, lambda r: (r / "README.md").write_text("touched\n", encoding="utf-8"))
    res = pickup.run(repo, say=_silent())
    assert res["picked"] == []
    assert res["rejected"] and "out-of-lane path README.md" in res["rejected"]
    assert pickup.load_state(repo)["applied_tip"] == ""
    # the rejection was journaled to the tracked ledger once
    ledger = (repo / ".court" / "LEDGER.md").read_text(encoding="utf-8")
    assert "pleasance pickup REJECTED" in ledger
    # nothing is ever copied into the working tree without a green batch
    assert not (repo / "README.md").exists()


def test_rejects_an_out_of_lane_court_file(tmp_path):
    repo = _repo(tmp_path)
    _please(repo, lambda r: (r / ".court" / "CONFIG_EXTRAS.md").write_text("x", encoding="utf-8"))
    res = pickup.run(repo, say=_silent())
    assert res["rejected"] and ".court/CONFIG_EXTRAS.md" in res["rejected"]
    assert pickup.load_state(repo)["applied_tip"] == ""


def test_rejects_an_unparseable_charter(tmp_path):
    repo = _repo(tmp_path)
    def builder(r):
        (r / ".court" / "quests").mkdir(parents=True, exist_ok=True)
        (r / ".court" / "quests" / "Q999-Broken.md").write_text(
            "this is not a quest charter, no frontmatter at all\n", encoding="utf-8")
    _please(repo, builder)
    res = pickup.run(repo, say=_silent())
    assert res["rejected"] and "unparseable charter" in res["rejected"]
    assert pickup.load_state(repo)["applied_tip"] == ""
    assert not (repo / ".court" / "quests" / "Q999-Broken.md").exists()


def test_rejects_an_invalid_prereq_predicate(tmp_path):
    repo = _repo(tmp_path)
    def builder(r):
        (r / ".court" / "quests").mkdir(parents=True, exist_ok=True)
        (r / ".court" / "quests" / "Q999-Bad-Prereq.md").write_text(
            _charter("Q999-Bad-Prereq", "Bad prereq", prereqs=["the vibes are good"]),
            encoding="utf-8")
    _please(repo, builder)
    res = pickup.run(repo, say=_silent())
    assert res["rejected"] and "invalid prerequisite" in res["rejected"]
    assert pickup.load_state(repo)["applied_tip"] == ""


def test_rejects_an_invalid_branch_frontmatter(tmp_path):
    repo = _repo(tmp_path)
    def builder(r):
        (r / ".court" / "quests").mkdir(parents=True, exist_ok=True)
        (r / ".court" / "quests" / "Q999-Flat-Branch.md").write_text(
            _charter("Q999-Flat-Branch", "Flat branch", branch="quest-q999-flat-branch"),
            encoding="utf-8")
    _please(repo, builder)
    res = pickup.run(repo, say=_silent())
    assert res["rejected"] and "invalid branch" in res["rejected"]
    assert pickup.load_state(repo)["applied_tip"] == ""


def test_rejects_edits_to_a_commissioned_charter(tmp_path):
    repo = _repo(tmp_path)
    quest_dir = repo / ".court" / "quests"
    quest_dir.mkdir(parents=True, exist_ok=True)
    (quest_dir / "Q101-Working.md").write_text(
        _charter("Q101-Working", "Already being worked", status="WORKING",
                 branch="quest/q101-working"),
        encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "commission Q101")
    _git(repo, "push", "-u", "origin", "castle")

    # A remote LLM re-writes the commissioned charter (tampering attempt).
    _please(repo, lambda r: (r / ".court" / "quests" / "Q101-Working.md").write_text(
        _charter("Q101-Working", "Already being worked (edited)", status="CHARTERED",
                 branch="quest/q101-working"),
        encoding="utf-8"))
    res = pickup.run(repo, say=_silent())
    assert res["rejected"] and "commissioned" in res["rejected"] and "frozen" in res["rejected"]
    assert pickup.load_state(repo)["applied_tip"] == ""
    # the base charter on castle is untouched
    on_disk = (repo / ".court" / "quests" / "Q101-Working.md").read_text(encoding="utf-8")
    assert "status: WORKING" in on_disk


def test_rejects_a_vigil_colliding_with_an_active_one_on_castle(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".court" / "vigils").mkdir(parents=True, exist_ok=True)
    spec = vigils.build(id="q900-after-q001", when=["after:2026-01-01T00:00:00Z"],
                        then="dispatch:Q900",
                        reason="Q900 needs Q001's schema on castle before dispatch.",
                        expires="6h")
    (repo / ".court" / "vigils" / "q900-after-q001.json").write_text(
        json.dumps(spec, indent=2), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "active vigil on castle")
    _git(repo, "push", "-u", "origin", "castle")

    def builder(r):
        updated = dict(spec)
        updated["reason"] = "updated by the away steward"
        (r / ".court" / "vigils" / "q900-after-q001.json").write_text(
            json.dumps(updated, indent=2), encoding="utf-8")
    _please(repo, builder)
    res = pickup.run(repo, say=_silent())
    assert res["rejected"] and "already active" in res["rejected"]
    assert pickup.load_state(repo)["applied_tip"] == ""


def test_dirty_working_tree_refuses_the_pickup(tmp_path):
    repo = _repo(tmp_path)
    _please(repo, _valid_builder)
    # dirty the checked-out tree (and add the ignored .kilo/tasks scaffold — it
    # must NOT trip the dirty gate on its own)
    (repo / ".kilo" / "tasks").mkdir(parents=True)
    (repo / ".kilo" / "tasks" / "pending.txt").write_text("x", encoding="utf-8")
    (repo / ".court" / "LEDGER.md").write_text("dirty edit\n", encoding="utf-8")
    res = pickup.run(repo, say=_silent())
    assert res["rejected"] and "dirty" in res["rejected"]
    assert pickup.load_state(repo)["applied_tip"] == ""
    assert not (repo / ".court" / "quests" / "Q999-Pickup-Lane.md").exists()


def test_dry_run_mutates_nothing(tmp_path):
    repo = _repo(tmp_path)
    _please(repo, _valid_builder)
    before = _git(repo, "rev-list", "--count", "HEAD").stdout.strip()
    res = pickup.run(repo, dry_run=True, say=_silent())
    assert res["dry_run"] is True
    assert res["rejected"] is None
    assert res["picked"] == [".court/quests/Q999-Pickup-Lane.md"]
    assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == before
    assert pickup.load_state(repo)["applied_tip"] == ""
    assert not (repo / ".court" / "quests" / "Q999-Pickup-Lane.md").exists()


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------


def test_accepts_a_single_chartered_new_quest(tmp_path):
    repo = _repo(tmp_path)
    _please(repo, _valid_builder)

    res = pickup.run(repo, say=_silent())
    assert res["rejected"] is None
    assert res["picked"] == [".court/quests/Q999-Pickup-Lane.md"]
    assert res["committed"]

    tip = _git(repo, "rev-parse", "pleasance").stdout.strip()
    assert pickup.load_state(repo)["applied_tip"] == tip

    # lands on the working tree of castle with the authoring kept intact
    md = (repo / ".court" / "quests" / "Q999-Pickup-Lane.md").read_text(encoding="utf-8")
    assert "status: CHARTERED" in md
    assert "branch: quest/q999-pickup-lane" in md

    # exactly one pickup commit on castle
    last = _git(repo, "log", "-1", "--format=%s").stdout.strip()
    assert last == "court: pleasance pickup 1 charter(s), 0 vigil(s)"

    # the working tree is clean afterwards and the ledger carries the line
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    ledger = (repo / ".court" / "LEDGER.md").read_text(encoding="utf-8")
    assert f"pleasance pickup applied {tip[:12]}" in ledger
    assert "1 charter(s), 0 vigil(s)" in ledger

    # NO vigil file was synthesized for the charter-driven dispatch lane
    vig_dir = repo / ".court" / "vigils"
    assert not vig_dir.exists() or not list(vig_dir.glob("*.json"))


def test_accepts_a_valid_vigil_file(tmp_path):
    repo = _repo(tmp_path)
    spec = vigils.build(id="q900-after-q001", when=["after:2026-01-01T00:00:00Z"],
                        then="dispatch:Q900",
                        reason="Q900 needs Q001's schema on castle before dispatch.",
                        expires="6h")

    def builder(r):
        (r / ".court" / "vigils").mkdir(parents=True, exist_ok=True)
        (r / ".court" / "vigils" / "q900-after-q001.json").write_text(
            json.dumps(spec, indent=2), encoding="utf-8")
    _please(repo, builder)

    res = pickup.run(repo, say=_silent())
    assert res["rejected"] is None
    assert res["picked"] == [".court/vigils/q900-after-q001.json"]
    loaded = json.loads((repo / ".court" / "vigils" / "q900-after-q001.json").read_text(encoding="utf-8"))
    assert loaded["id"] == "q900-after-q001" and loaded["status"] == "active"


def test_nothing_new_is_a_no_op(tmp_path):
    repo = _repo(tmp_path)
    res = pickup.run(repo, say=_silent())
    assert res["rejected"] is None
    assert res["skipped"] == "no pleasance branch"
    assert pickup.load_state(repo)["applied_tip"] == ""


def test_incremental_second_pickup_and_idempotence(tmp_path):
    repo = _repo(tmp_path)
    _please(repo, _valid_builder)
    res1 = pickup.run(repo, say=_silent())
    tip1 = _git(repo, "rev-parse", "pleasance").stdout.strip()
    assert res1["rejected"] is None and res1["picked"]
    assert pickup.load_state(repo)["applied_tip"] == tip1

    # running again with no new work is a no-op and adds no commits
    commits_before = _git(repo, "rev-list", "--count", "HEAD").stdout.strip()
    res_noop = pickup.run(repo, say=_silent())
    assert res_noop["skipped"] == "already applied"
    assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == commits_before

    # a second commit lands only the new file, from the previous applied tip
    def builder2(r):
        (r / ".court" / "quests").mkdir(parents=True, exist_ok=True)
        (r / ".court" / "quests" / "Q998-Second.md").write_text(
            _charter("Q998-Second", "Second wave", status="PLANNED", branch="quest/q998-second"),
            encoding="utf-8")
    _please_again(repo, builder2)
    res2 = pickup.run(repo, say=_silent())
    assert res2["rejected"] is None
    assert res2["picked"] == [".court/quests/Q998-Second.md"]
    tip2 = _git(repo, "rev-parse", "pleasance").stdout.strip()
    assert pickup.load_state(repo)["applied_tip"] == tip2

def test_accepts_charter_with_its_sibling_events_journal(tmp_path):
    """A charter and the quest's standard `<id>.events.jsonl` journal are one
    in-lane batch (the Q1141 shape); both land on castle together, and no
    synthesized vigil file is created."""
    repo = _repo(tmp_path)

    def builder(r):
        (r / ".court" / "quests").mkdir(parents=True, exist_ok=True)
        (r / ".court" / "quests" / "Q999-Pickup-Lane.md").write_text(
            _charter("Q999-Pickup-Lane", "Events sibling demo",
                     status="CHARTERED", branch="quest/q999-pickup-lane"),
            encoding="utf-8")
        (r / ".court" / "quests" / "Q999-Pickup-Lane.events.jsonl").write_text(
            '{"target":"field:id","ts":"2026-11-04T19:27:00Z","value":"Q999-Pickup-Lane"}\n'
            '{"target":"field:title","ts":"2026-11-04T19:27:00Z","value":"Events sibling demo"}\n',
            encoding="utf-8")

    _please(repo, builder)
    res = pickup.run(repo, say=_silent())
    assert res.get("rejected") is None, res
    assert ".court/quests/Q999-Pickup-Lane.md" in res.get("picked", [])
    # the sibling journal rides the same atomic commit
    assert ".court/quests/Q999-Pickup-Lane.events.jsonl" in res.get("picked", [])
    assert (repo / ".court" / "quests" / "Q999-Pickup-Lane.md").exists()
    assert (repo / ".court" / "quests" / "Q999-Pickup-Lane.events.jsonl").exists()
    # charter-driven lane: no vigil file for this quest
    assert not (repo / ".court" / "vigils" / "dispatch-q999.json").exists()


def test_events_journal_must_be_json_lines(tmp_path):
    """A malformed events journal rejects the batch (no laundering junk)."""
    repo = _repo(tmp_path)

    def builder(r):
        (r / ".court" / "quests").mkdir(parents=True, exist_ok=True)
        (r / ".court" / "quests" / "Q999-Pickup-Lane.md").write_text(
            _charter("Q999-Pickup-Lane", "Events sibling demo"),
            encoding="utf-8")
        (r / ".court" / "quests" / "Q999-Pickup-Lane.events.jsonl").write_text(
            "this is not json at all\n", encoding="utf-8")

    _please(repo, builder)
    res = pickup.run(repo, say=_silent())
    assert res.get("rejected") and "events journal" in res.get("rejected", "")


def test_diff_is_taken_against_the_fork_point_not_castle_tip(tmp_path):
    """A pleasance batch is judged on the remote author's OWN commits: castle
    racing dozens of commits ahead (the real-world 1062-deep lead) must not
    leak hundreds of unrelated 'reverse' paths into the lane."""
    repo = _repo(tmp_path)
    _please(repo, _valid_builder)

    # Castle races far ahead with unrelated work AFTER the fork point.
    for i in range(6):
        (repo / f"unrelated-{i}.py").write_text(f"x = {i}\n", encoding="utf-8")
        (repo / ".court" / "EDICTS.md").write_text(
            f"- **edict {i}**\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", f"castle races ahead {i}")

    res = pickup.run(repo, say=_silent())
    assert res.get("rejected") is None, res
    assert res.get("picked") == [".court/quests/Q999-Pickup-Lane.md"]
    # the far-ahead castle churn was never part of the batch
    assert (repo / ".court" / "quests" / "Q999-Pickup-Lane.md").exists()
