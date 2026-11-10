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

from court import nightwatch
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
    repo.mkdir(parents=True)
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


def test_accepts_a_none_sentinel_in_prerequisites(tmp_path):
    """A remote author writing "None." under `## Prerequisites` means "no
    preconditions" — it must not reject the whole pickup batch as a bad
    predicate."""
    repo = _repo(tmp_path)
    def builder(r):
        (r / ".court" / "quests").mkdir(parents=True, exist_ok=True)
        (r / ".court" / "quests" / "Q999-No-Prereqs.md").write_text(
            _charter("Q999-No-Prereqs", "No prereqs", prereqs=["None."]),
            encoding="utf-8")
    _please(repo, builder)
    res = pickup.run(repo, say=_silent())
    assert not res["rejected"], res["rejected"]
    assert (repo / ".court" / "quests" / "Q999-No-Prereqs.md").exists()


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


def test_skips_edits_to_a_commissioned_charter(tmp_path):
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

    # A remote LLM re-writes the commissioned charter (tampering attempt). The
    # file is superseded and skipped; the lane itself must not wedge.
    _please(repo, lambda r: (r / ".court" / "quests" / "Q101-Working.md").write_text(
        _charter("Q101-Working", "Already being worked (edited)", status="CHARTERED",
                 branch="quest/q101-working"),
        encoding="utf-8"))
    res = pickup.run(repo, say=_silent())
    assert res["rejected"] is None
    assert res["picked"] == []
    assert any("Q101-Working.md" in i["path"] and "frozen" in i["reason"]
               for i in res["skipped_files"])
    assert pickup.load_state(repo)["applied_tip"] != ""
    # the base charter on castle is untouched
    on_disk = (repo / ".court" / "quests" / "Q101-Working.md").read_text(encoding="utf-8")
    assert "status: WORKING" in on_disk


def test_skips_a_frozen_charter_but_accepts_the_rest_of_the_batch(tmp_path):
    """The incident shape: a re-scoped commissioned charter shares a batch with
    brand-new charters. The frozen file is skipped; the new work still lands."""
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

    def builder(r):
        (r / ".court" / "quests" / "Q101-Working.md").write_text(
            _charter("Q101-Working", "Already being worked (edited)", status="CHARTERED",
                     branch="quest/q101-working"),
            encoding="utf-8")
        _valid_builder(r)

    _please(repo, builder)
    res = pickup.run(repo, say=_silent())
    assert res["rejected"] is None
    assert res["picked"] == [".court/quests/Q999-Pickup-Lane.md"]
    assert any("Q101-Working.md" in i["path"] for i in res["skipped_files"])
    on_disk = (repo / ".court" / "quests" / "Q101-Working.md").read_text(encoding="utf-8")
    assert "status: WORKING" in on_disk
    assert (repo / ".court" / "quests" / "Q999-Pickup-Lane.md").exists()


def test_skips_a_vigil_colliding_with_an_active_one_on_castle(tmp_path):
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
    assert res["rejected"] is None
    assert res["picked"] == []
    assert any("already active" in i["reason"] for i in res["skipped_files"])
    assert pickup.load_state(repo)["applied_tip"] != ""
    # the active vigil on castle keeps its original reason
    on_disk = json.loads((repo / ".court" / "vigils" / "q900-after-q001.json").read_text(encoding="utf-8"))
    assert on_disk["reason"].startswith("Q900 needs")


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


# ---------------------------------------------------------------------------
# Royal decrees — M'Lord answers the Audience frontier from the pleasance lane
# ---------------------------------------------------------------------------


def _decree(decree_id="decree-1", quest="Q123-Held", ruling="Use session cookies.",
            status="WORKING"):
    decision = {"quest": quest, "ruling": ruling}
    if status:
        decision["status"] = status
    return json.dumps({
        "id": decree_id,
        "by": "M'Lord",
        "source": "voice via the Pleasance agent",
        "decisions": [decision],
    }, indent=2)


def _held_quest(repo, qid="Q123-Held"):
    quest_dir = repo / ".court" / "quests"
    quest_dir.mkdir(parents=True, exist_ok=True)
    (quest_dir / f"{qid}.md").write_text(
        _charter(qid, "Held thing", status="HELD", branch="quest/q123-held"),
        encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"charter {qid}")
    _git(repo, "push", "-u", "origin", "castle")


def _decree_builder(packet, name="decree-1"):
    def builder(r):
        d = r / ".court" / "decrees"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.json").write_text(packet, encoding="utf-8")
    return builder


def test_applies_a_decree_packet_and_unblocks_the_quest(tmp_path):
    repo = _repo(tmp_path)
    _held_quest(repo)
    _please(repo, _decree_builder(_decree()))

    res = pickup.run(repo, say=_silent())
    assert res["rejected"] is None
    assert res["picked"] == [".court/decrees/decree-1.json"]
    assert len(res["decrees"]) == 1
    assert res["decrees"][0]["from"] == "HELD"
    assert res["decrees"][0]["to"] == "WORKING"

    charter = (repo / ".court" / "quests" / "Q123-Held.md").read_text(encoding="utf-8")
    assert "status: WORKING" in charter
    assert "Royal Decree `decree-1`" in charter
    assert "Use session cookies." in charter
    # the ruling is durable paperwork on castle, and the applied id is recorded
    assert (repo / ".court" / "decrees" / "decree-1.json").exists()
    assert "decree-1" in pickup.load_state(repo)["applied_decrees"]

    ledger = (repo / ".court" / "LEDGER.md").read_text(encoding="utf-8")
    assert "decree ruling(s)" in ledger
    last = _git(repo, "log", "-1", "--format=%s").stdout.strip()
    assert "decree ruling(s)" in last


def test_skips_a_decree_packet_that_was_already_applied(tmp_path):
    repo = _repo(tmp_path)
    _held_quest(repo)
    _please(repo, _decree_builder(_decree()))
    assert pickup.run(repo, say=_silent())["decrees"]

    # a re-delivery of the SAME decree id (a rebase or a duplicate commit)
    def redo(r):
        (r / ".court" / "decrees" / "decree-1.json").write_text(
            _decree(ruling="Use session cookies, delivered again."), encoding="utf-8")
    _please_again(repo, redo)

    res = pickup.run(repo, say=_silent())
    assert res["rejected"] is None
    assert res["picked"] == []
    assert res["decrees"] == []
    assert any("already applied" in i["reason"] for i in res["skipped_files"])
    # the original ruling is not duplicated into the Audience Log
    charter = (repo / ".court" / "quests" / "Q123-Held.md").read_text(encoding="utf-8")
    assert charter.count("Royal Decree `decree-1`") == 1


def test_skips_a_decree_for_an_unknown_quest_without_wedging_the_lane(tmp_path):
    repo = _repo(tmp_path)
    _held_quest(repo)

    def builder(r):
        d = r / ".court" / "decrees"
        d.mkdir(parents=True, exist_ok=True)
        (d / "decree-1.json").write_text(_decree(quest="Q999-Ghost"), encoding="utf-8")
        # a healthy charter rides the SAME batch: one unusable decree must not
        # hold the paperwork lane hostage
        q = r / ".court" / "quests" / "Q998-Also-Fine.md"
        q.write_text(_charter("Q998-Also-Fine", "Fine thing"), encoding="utf-8")
    _please(repo, builder)

    res = pickup.run(repo, say=_silent())
    assert res["rejected"] is None
    assert res["decrees"] == []
    assert res["picked"] == [".court/quests/Q998-Also-Fine.md"]
    assert any("unknown quest" in i["reason"] for i in res["skipped_files"])
    # nothing was applied to the wrong quest and no decree file landed
    assert not (repo / ".court" / "decrees" / "decree-1.json").exists()
    charter = (repo / ".court" / "quests" / "Q123-Held.md").read_text(encoding="utf-8")
    assert "status: HELD" in charter


def test_skips_a_decree_that_tries_to_raze(tmp_path):
    repo = _repo(tmp_path)
    _held_quest(repo)
    _please(repo, _decree_builder(_decree(status="READY_TO_RAZE")))
    res = pickup.run(repo, say=_silent())
    assert res["rejected"] is None
    assert any("human acts" in i["reason"] for i in res["skipped_files"])
    charter = (repo / ".court" / "quests" / "Q123-Held.md").read_text(encoding="utf-8")
    assert "status: HELD" in charter


def test_expands_a_multi_quest_decree_into_one_ruling_per_target(tmp_path):
    repo = _repo(tmp_path)
    _held_quest(repo, "Q123-Held")
    _held_quest(repo, "Q124-Held")
    quest = "Q123-Held,Q124-Held"
    _please(repo, _decree_builder(_decree(quest=quest)))

    res = pickup.run(repo, say=_silent())
    assert res["rejected"] is None
    assert len(res["decrees"]) == 2
    assert {d["quest"] for d in res["decrees"]} == {"Q123-Held", "Q124-Held"}
    for qid in ("Q123-Held", "Q124-Held"):
        charter = (repo / ".court" / "quests" / f"{qid}.md").read_text(encoding="utf-8")
        assert "status: WORKING" in charter
        assert "Use session cookies." in charter


def test_a_multi_quest_decree_with_one_dead_id_is_skipped_whole(tmp_path):
    repo = _repo(tmp_path)
    _held_quest(repo, "Q123-Held")
    _please(repo, _decree_builder(_decree(quest="Q123-Held,Q999-Ghost")))
    res = pickup.run(repo, say=_silent())
    assert res["rejected"] is None
    assert res["decrees"] == []
    assert any("Q999-Ghost" in i["reason"] for i in res["skipped_files"])
    # a ruling is never applied to a partial target set
    charter = (repo / ".court" / "quests" / "Q123-Held.md").read_text(encoding="utf-8")
    assert "status: HELD" in charter


# ---------------------------------------------------------------------------
# Rejection classification — stable machine-readable classes + path extraction
# ---------------------------------------------------------------------------


def test_out_of_lane_batch_classifies_as_non_paperwork_commit(tmp_path):
    repo = _repo(tmp_path)
    _please(repo, lambda r: (r / "README.md").write_text("touched\n", encoding="utf-8"))
    res = pickup.run(repo, say=_silent())
    assert res["rejection_class"] == "non-paperwork-commit"
    assert res["rejection_paths"] == ["README.md"]
    # the human text callers/tests assert on is unchanged
    assert res["rejected"].startswith("out-of-lane path README.md in pleasance batch — only ")


def test_invalid_charter_classes(tmp_path):
    repo = _repo(tmp_path)

    def builder(r):
        (r / ".court" / "quests").mkdir(parents=True, exist_ok=True)
        (r / ".court" / "quests" / "Q999-Broken.md").write_text(
            "this is not a quest charter, no frontmatter at all\n", encoding="utf-8")
    _please(repo, builder)
    res = pickup.run(repo, say=_silent())
    assert res["rejection_class"] == "invalid-charter"
    assert res["rejection_paths"] == [".court/quests/Q999-Broken.md"]
    assert "unparseable charter" in res["rejected"]


def test_invalid_prereq_and_branch_are_invalid_charter(tmp_path):
    repo = _repo(tmp_path)

    def builder(r):
        (r / ".court" / "quests").mkdir(parents=True, exist_ok=True)
        (r / ".court" / "quests" / "Q999-Bad-Prereq.md").write_text(
            _charter("Q999-Bad-Prereq", "Bad prereq", prereqs=["the vibes are good"]),
            encoding="utf-8")
    _please(repo, builder)
    res = pickup.run(repo, say=_silent())
    assert res["rejection_class"] == "invalid-charter"
    assert "invalid prerequisite" in res["rejected"]

    other = _repo(tmp_path / "branch-case")

    def branch_builder(r):
        (r / ".court" / "quests").mkdir(parents=True, exist_ok=True)
        (r / ".court" / "quests" / "Q999-Flat-Branch.md").write_text(
            _charter("Q999-Flat-Branch", "Flat branch", branch="quest-q999-flat-branch"),
            encoding="utf-8")
    _please(other, branch_builder)
    res = pickup.run(other, say=_silent())
    assert res["rejection_class"] == "invalid-charter"
    assert res["rejection_paths"] == [".court/quests/Q999-Flat-Branch.md"]
    assert "invalid branch" in res["rejected"]


def test_events_journal_failure_is_invalid_charter(tmp_path):
    repo = _repo(tmp_path)

    def builder(r):
        (r / ".court" / "quests").mkdir(parents=True, exist_ok=True)
        (r / ".court" / "quests" / "Q999-Pickup-Lane.md").write_text(
            _charter("Q999-Pickup-Lane", "Events sibling demo"), encoding="utf-8")
        (r / ".court" / "quests" / "Q999-Pickup-Lane.events.jsonl").write_text(
            "this is not json at all\n", encoding="utf-8")
    _please(repo, builder)
    res = pickup.run(repo, say=_silent())
    assert res["rejection_class"] == "invalid-charter"
    assert res["rejection_paths"] == [".court/quests/Q999-Pickup-Lane.events.jsonl"]


def test_dirty_tracked_and_untracked_classes(tmp_path):
    # tracked: the pickup itself appends to .court/LEDGER.md
    tracked_repo = _repo(tmp_path / "tracked")
    _please(tracked_repo, _valid_builder)
    (tracked_repo / ".court" / "LEDGER.md").write_text("dirty edit\n", encoding="utf-8")
    res = pickup.run(tracked_repo, say=_silent())
    assert res["rejection_class"] == "dirty-tracked"
    assert res["rejection_paths"] == [".court/LEDGER.md"]
    assert res["rejected"] == (
        "castle working tree has conflicting dirty path(s) (.court/LEDGER.md); "
        "refusing to mix the pickup commit")

    # untracked: the real incident shape (scripts/db/roq_prod.py)
    untracked_repo = _repo(tmp_path / "untracked")
    _please(untracked_repo, _valid_builder)
    (untracked_repo / "scripts" / "db").mkdir(parents=True)
    (untracked_repo / "scripts" / "db" / "roq_prod.py").write_text("x = 1\n", encoding="utf-8")
    res = pickup.run(untracked_repo, say=_silent())
    assert res["rejection_class"] == "dirty-untracked"
    assert res["rejection_paths"] == ["scripts/db/roq_prod.py"]


def test_invalid_vigil_classifies_as_a_batch_rejection(tmp_path):
    repo = _repo(tmp_path)

    def vigil_builder(r):
        (r / ".court" / "vigils").mkdir(parents=True, exist_ok=True)
        (r / ".court" / "vigils" / "broken.json").write_text("{not json", encoding="utf-8")
    _please(repo, vigil_builder)
    res = pickup.run(repo, say=_silent())
    assert res["rejection_class"] == "invalid-vigil"
    assert res["rejection_paths"] == [".court/vigils/broken.json"]


def test_an_unusable_decree_is_skipped_but_never_classified_as_a_batch_failure(tmp_path):
    """A malformed decree file is held back on its own (per-file supersession).
    It carries no `rejection_class` because the lane as a whole is not blocked."""
    repo = _repo(tmp_path)
    _held_quest(repo)
    _please(repo, _decree_builder(_decree(quest="Q999-Ghost")))
    res = pickup.run(repo, say=_silent())
    assert res["rejected"] is None
    assert res["rejection_class"] is None
    assert res["decrees"] == []
    assert any("unknown quest" in i["reason"] for i in res["skipped_files"])


def test_pickup_remedy_is_pure_and_non_destructive():
    text = pickup.pickup_remedy("dirty-untracked", ["scripts/db/roq_prod.py"])
    assert "scripts/db/roq_prod.py" in text
    assert "never `git clean`" in text
    assert "git clean -" not in text.replace("never `git clean`", "")
    assert "commit or stash" in pickup.pickup_remedy("dirty-tracked", ["x.py"])
    assert "pleasance" in pickup.pickup_remedy("non-paperwork-commit", ["README.md"])
    assert "branch" in pickup.pickup_remedy("invalid-charter", ["a.md"])
    assert "immutable" in pickup.pickup_remedy("charter-tamper", ["a.md"])
    assert "unknown" in pickup.pickup_remedy("not-a-class", [])


# ---------------------------------------------------------------------------
# Pickup streak — increment, reset, clear, and milestone-only journaling
# ---------------------------------------------------------------------------


def _pickup_events(repo):
    path = nightwatch.journal_path(repo)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") == "pickup":
            out.append(event)
    return out


def _untracked_blocker(repo):
    (repo / "scripts" / "db").mkdir(parents=True, exist_ok=True)
    (repo / "scripts" / "db" / "roq_prod.py").write_text("x = 1\n", encoding="utf-8")


def test_pickup_streak_increments_resets_and_clears(tmp_path):
    repo = _repo(tmp_path)
    _please(repo, _valid_builder)
    _untracked_blocker(repo)
    state = {}
    for _ in range(5):
        nightwatch._run_pickup(repo, state, dry_run=False, say=_silent())

    streak = state["pickup_streak"]
    assert streak["count"] == 5
    assert streak["class"] == "dirty-untracked"
    assert streak["paths"] == ["scripts/db/roq_prod.py"]
    assert state["last_pickup"]["streak_count"] == 5
    assert state["last_pickup"]["rejection_class"] == "dirty-untracked"
    assert "never `git clean`" in state["last_pickup"]["remedy"]

    # journal only at the streak start and the milestone, never every tick
    assert [e["pickup_streak"] for e in _pickup_events(repo)] == [1, 3]

    # class/paths change resets the streak to 1 with a fresh start
    (repo / "scripts" / "db" / "roq_prod.py").unlink()
    _please_again(repo, lambda r: (r / "README.md").write_text("touched\n", encoding="utf-8"))
    nightwatch._run_pickup(repo, state, dry_run=False, say=_silent())
    assert state["pickup_streak"]["count"] == 1
    assert state["pickup_streak"]["class"] == "non-paperwork-commit"
    assert state["pickup_streak"]["paths"] == ["README.md"]
    assert "remedy" not in state["last_pickup"]

    # resolving the lane (the out-of-lane path is deleted) clears the streak
    _please_again(repo, lambda r: (r / "README.md").unlink())
    result = nightwatch._run_pickup(repo, state, dry_run=False, say=_silent())
    assert result.get("rejected") is None
    assert "pickup_streak" not in state
    assert state["last_pickup"]["rejected"] is None


def test_pickup_streak_milestones_are_the_only_journal_lines(tmp_path):
    repo = _repo(tmp_path)
    _please(repo, _valid_builder)
    _untracked_blocker(repo)
    state = {}
    for _ in range(12):
        nightwatch._run_pickup(repo, state, dry_run=False, say=_silent())
    assert state["pickup_streak"]["count"] == 12
    assert [e["pickup_streak"] for e in _pickup_events(repo)] == [1, 3, 10]
    # the milestone line carries the remedy for the remote reader
    assert "never `git clean`" in _pickup_events(repo)[-1]["remedy"]


def test_pickup_streak_no_op_and_off_trunk_clear_it(tmp_path):
    repo = _repo(tmp_path)
    state = {"pickup_streak": {"count": 4, "since": "x", "class": "dirty-untracked",
                               "paths": ["a"], "last_ts": "x"}}
    # no pleasance branch at all is a successful no-op
    result = nightwatch._run_pickup(repo, state, dry_run=False, say=_silent())
    assert result.get("rejected") is None
    assert "pickup_streak" not in state


# ---------------------------------------------------------------------------
# Mechanical remediation — the watch clears its own dirty-tree block
# ---------------------------------------------------------------------------


def _scratch_blocker(repo, rel="tasks/artifacts/q043/sql/01_counts.sql"):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("select 1;\n", encoding="utf-8")
    return rel


def test_remediation_plan_quarantines_scratch_and_refuses_real_edits(tmp_path):
    repo = _repo(tmp_path)
    scratch = _scratch_blocker(repo)
    plan = pickup.remediation_plan(repo, "dirty-untracked", [scratch])
    assert plan["ok"] and plan["action"] == "quarantine"
    assert plan["untracked"] == [scratch]

    real = repo / "scripts" / "db" / "roq_prod.py"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("x = 1\n", encoding="utf-8")
    plan = pickup.remediation_plan(repo, "dirty-untracked", ["scripts/db/roq_prod.py"])
    assert not plan["ok"]
    assert "not mechanically clearable" in plan["reason"]

    # only the two dirty classes are mechanically addressable
    assert not pickup.remediation_plan(repo, "invalid-charter", [scratch])["ok"]


def test_remediate_moves_scratch_out_of_the_tree_and_unblocks_pickup(tmp_path):
    repo = _repo(tmp_path)
    _please(repo, _valid_builder)
    scratch = _scratch_blocker(repo)
    res = pickup.run(repo, say=_silent())
    assert res["rejection_class"] == "dirty-untracked"

    verdict = pickup.remediate(repo, res["rejection_class"], res["rejection_paths"],
                               say=_silent())
    assert verdict["ok"] and verdict["action"] == "quarantine"
    assert verdict["moved"] == [scratch]
    # the evidence is preserved OUTSIDE the tree (never deleted)
    parked = Path(verdict["quarantine_dir"]) / scratch
    assert parked.read_text(encoding="utf-8") == "select 1;\n"
    assert not (repo / scratch).exists()

    applied = pickup.run(repo, say=_silent())
    assert applied["rejected"] is None
    assert ".court/quests/Q999-Pickup-Lane.md" in applied["picked"]


def test_remediate_restores_a_regenerable_tracked_report(tmp_path):
    repo = _repo(tmp_path)
    report = repo / "docs" / "PUNISHED_AUDIT.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("Generated: HEAD report\n", encoding="utf-8")
    _git(repo, "add", "docs/PUNISHED_AUDIT.md")
    _git(repo, "commit", "-m", "docs: audit report")
    _please(repo, _valid_builder)
    report.write_text("Generated: uncommitted regeneration\n", encoding="utf-8")

    res = pickup.run(repo, say=_silent())
    assert res["rejection_class"] == "dirty-tracked"
    verdict = pickup.remediate(repo, res["rejection_class"], res["rejection_paths"],
                               say=_silent())
    assert verdict["ok"] and verdict["action"] == "restore"
    assert verdict["restored"] == ["docs/PUNISHED_AUDIT.md"]
    assert report.read_text(encoding="utf-8") == "Generated: HEAD report\n"
    assert verdict["moved"] == []


def test_maybe_remediate_pickup_retries_and_clears_the_streak(tmp_path):
    repo = _repo(tmp_path)
    _please(repo, _valid_builder)
    _scratch_blocker(repo)
    state = {}
    for _ in range(2):
        nightwatch._run_pickup(repo, state, dry_run=False, say=_silent())
    assert state["pickup_streak"]["count"] == 2

    record = nightwatch._maybe_remediate_pickup(
        repo, state, nightwatch.Policy(), dry_run=False, say=_silent())
    assert record and record["ok"] and record["action"] == "quarantine"
    # the retry applied the batch, so the streak is gone
    assert "pickup_streak" not in state
    assert state["last_pickup"]["rejected"] is None
    assert ".court/quests/Q999-Pickup-Lane.md" in state["last_pickup"]["picked"]
    assert state["pickup_remedy"]["ok"] is True
    # journaled once, with the moved path spelled out
    events = [json.loads(l) for l in
              nightwatch.journal_path(repo).read_text(encoding="utf-8").splitlines()]
    remedy_events = [e for e in events if e.get("event") == "pickup_remedy"]
    assert len(remedy_events) == 1
    assert remedy_events[0]["moved"] == ["tasks/artifacts/q043/sql/01_counts.sql"]


def test_maybe_remediate_pickup_respects_threshold_policy_and_cooldown(tmp_path):
    repo = _repo(tmp_path)
    _please(repo, _valid_builder)
    _scratch_blocker(repo)
    state = {}
    nightwatch._run_pickup(repo, state, dry_run=False, say=_silent())

    # below the threshold: one rejection is not yet treated as a wedge
    assert nightwatch._maybe_remediate_pickup(
        repo, state, nightwatch.Policy(pickup_remedy_after=2),
        dry_run=False, say=_silent()) is None

    # a disabled policy never touches the tree
    blocked = nightwatch._maybe_remediate_pickup(
        repo, state, nightwatch.Policy(pickup_remedy=False),
        dry_run=False, say=_silent())
    assert blocked is None
    assert (repo / "tasks/artifacts/q043/sql/01_counts.sql").exists()

    # after remediation succeeds the retry runs; a second call inside the
    # cooldown is a no-op (the lane is already clear)
    state["pickup_streak"] = {"count": 2, "since": "x", "class": "dirty-untracked",
                              "paths": ["tasks/artifacts/q043/sql/01_counts.sql"]}
    nightwatch._maybe_remediate_pickup(repo, state, nightwatch.Policy(),
                                       dry_run=False, say=_silent())
    assert nightwatch._maybe_remediate_pickup(
        repo, state, nightwatch.Policy(), dry_run=False, say=_silent()) is None


def _disable_lane_charters(repo):
    cfg = repo / ".court" / "config.json"
    data = {}
    if cfg.exists():
        data = json.loads(cfg.read_text(encoding="utf-8"))
    data.setdefault("pickup", {})["allow_lane_charters"] = False
    cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    _git(repo, "add", ".court/config.json")
    _git(repo, "commit", "-m", "court: requests-in, charters-out")


def test_lane_charters_are_refused_when_the_repo_forbids_them(tmp_path):
    repo = _repo(tmp_path)
    _disable_lane_charters(repo)
    _please(repo, _valid_builder)
    res = pickup.run(repo, say=_silent())
    assert res["picked"] == []
    assert "may not author charters" in (res["rejected"] or "")
    # The rejection names the replacement, so the remote author can self-correct.
    assert ".court/requests/" in (res["rejected"] or "")


def test_a_request_packet_lands_on_castle(tmp_path):
    repo = _repo(tmp_path)
    _disable_lane_charters(repo)

    def builder(r):
        (r / ".court" / "requests").mkdir(parents=True, exist_ok=True)
        (r / ".court" / "requests" / "req-thing.json").write_text(json.dumps({
            "id": "req-thing",
            "title": "Ship the thing",
            "ask": "Do the thing end to end, with tests.",
            "requested_by": "voice-agent",
        }), encoding="utf-8")

    _please(repo, builder)
    res = pickup.run(repo, say=_silent())
    assert res["rejected"] is None, res["rejected"]
    assert res["picked"] == [".court/requests/req-thing.json"]
    landed = (repo / ".court" / "requests" / "req-thing.json")
    assert landed.is_file()
    assert "req-thing" in landed.read_text(encoding="utf-8")
