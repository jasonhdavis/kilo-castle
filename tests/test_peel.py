"""Tests for gatehouse reversibility: `court peel` + re-pack.

The shape reproduced here is the post-promotion withdrawal: two Quests are
merged into a convoy, the convoy is promoted into ``castle``, and one Quest is
then withdrawn. Peeling must remove only that Quest's code, record the
withdrawal, and let a re-pack reinstate the invariant.
"""
import json
import subprocess

import pytest

from court import convoy_guard as cg
from court import peel, store


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "peel@test")
    _git(repo, "config", "user.name", "peel")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "castle")
    (repo / ".court" / "quests").mkdir(parents=True)
    (repo / ".court" / "quests" / "Q1009-Test.md").write_text("---\nid: Q1009-Test\ntitle: Orders\nbranch: quest/q1009-orders\nstatus: READY_TO_RAZE\n---\n")
    (repo / ".court" / "quests" / "Q1013-Test.md").write_text(
        "---\nid: Q1013-Test\ntitle: Marketing\nbranch: quest/q1013-marketing\nstatus: PUNISHED\ncogship_id: cogship-1\n---\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "charters")
    return repo


def _quest_branch(repo, branch, filename):
    _git(repo, "checkout", "-b", branch, "castle")
    (repo / filename).write_text("work\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"work on {branch}")
    _git(repo, "checkout", "castle")


def _promote(repo):
    convoy = cg.convoy_branch("cogship-1")
    _git(repo, "checkout", "-b", convoy, "castle")
    _git(repo, "merge", "--no-ff", "quest/q1009-orders", "-m", f"Merge branch 'quest/q1009-orders' into {convoy}")
    _git(repo, "merge", "--no-ff", "quest/q1013-marketing", "-m", f"Merge branch 'quest/q1013-marketing' into {convoy}")
    _git(repo, "checkout", "castle")
    _git(repo, "merge", "--no-ff", convoy, "-m", f"Merge {convoy} into castle")
    tip = _git(repo, "rev-parse", convoy).stdout.strip()
    charter = repo / ".court" / "quests" / "Q1013-Test.md"
    charter.write_text(charter.read_text().replace("cogship_id: cogship-1", f"cogship_id: cogship-1\ncogship_promoted_commit: {tip}"))
    proof = repo / ".court" / "suites" / f"{cg.normalize_cogship_id('cogship-1')}.json"
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text(json.dumps({"cogship_id": "cogship-1", "head_sha": "deadbeef", "exit_code": 0}))
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "suite proof")


def _load(repo, qid):
    return store.load(qid, court_root=repo / ".court")


def test_peel_reverts_only_the_withdrawn_quest(tmp_path):
    repo = _repo(tmp_path)
    _quest_branch(repo, "quest/q1009-orders", "nine.txt")
    _quest_branch(repo, "quest/q1013-marketing", "thirteen.txt")
    _promote(repo)
    assert (repo / "thirteen.txt").exists()

    result = peel.peel_quest(repo, _load(repo, "Q1013"), reason="royal ruling")

    assert result["ok"] and result["revert_sha"]
    assert not (repo / "thirteen.txt").exists()
    assert (repo / "nine.txt").exists()
    record = cg.load_peel_record(repo, "Q1013")
    assert record["reason"] == "royal ruling"
    assert record["revert_sha"] == result["revert_sha"]
    assert "Q1013" in cg.peeled_quest_ids(repo, "castle")


def test_peel_supersedes_the_convoy_suite_proof(tmp_path):
    repo = _repo(tmp_path)
    _quest_branch(repo, "quest/q1009-orders", "nine.txt")
    _quest_branch(repo, "quest/q1013-marketing", "thirteen.txt")
    _promote(repo)

    peel.peel_quest(repo, _load(repo, "Q1013"))

    assert not (repo / ".court" / "suites" / f"{cg.normalize_cogship_id('cogship-1')}.json").exists()
    superseded = list((repo / ".court" / "suites").glob(f"{cg.normalize_cogship_id('cogship-1')}.json.superseded-*"))
    assert len(superseded) == 1


def test_a_peeled_quest_no_longer_trips_the_trunk_invariant(tmp_path):
    repo = _repo(tmp_path)
    _quest_branch(repo, "quest/q1009-orders", "nine.txt")
    _quest_branch(repo, "quest/q1013-marketing", "thirteen.txt")
    _promote(repo)
    assert [f.quest for f in cg.unpromoted_quests_in_trunk(repo, trunk="castle")] == ["Q1013"]

    peel.peel_quest(repo, _load(repo, "Q1013"))

    assert cg.unpromoted_quests_in_trunk(repo, trunk="castle") == []


def test_re_packing_reinstates_the_quest(tmp_path):
    repo = _repo(tmp_path)
    _quest_branch(repo, "quest/q1009-orders", "nine.txt")
    _quest_branch(repo, "quest/q1013-marketing", "thirteen.txt")
    _promote(repo)
    peel.peel_quest(repo, _load(repo, "Q1013"))
    assert cg.peeled_quest_ids(repo, "castle") == {"Q1013"}

    cg.record_pack(repo, "cogship-2", ["Q1013-Test"])

    assert cg.peeled_quest_ids(repo, "castle") == set()
    assert cg.load_peel_record(repo, "Q1013") == {}
    assert [f.quest for f in cg.unpromoted_quests_in_trunk(repo, trunk="castle")] == ["Q1013"]


def test_peel_resolves_when_the_branch_tip_moved_after_promotion(tmp_path):
    repo = _repo(tmp_path)
    _quest_branch(repo, "quest/q1009-orders", "nine.txt")
    _quest_branch(repo, "quest/q1013-marketing", "thirteen.txt")
    _promote(repo)
    # Court bookkeeping after promotion: the branch tip is no longer an ancestor
    # of the trunk, but the promoted code still is.
    _git(repo, "checkout", "quest/q1013-marketing")
    (repo / "q1013-side-state.txt").write_text("ledger\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "court: sync royal side-state")
    _git(repo, "checkout", "castle")
    assert subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", "quest/q1013-marketing", "castle"]
    ).returncode == 1

    result = peel.peel_quest(repo, _load(repo, "Q1013"))

    assert result["ok"]
    assert not (repo / "thirteen.txt").exists()
    assert (repo / "nine.txt").exists()


def test_a_bookkeeping_conflict_is_kept_while_code_is_removed(tmp_path):
    repo = _repo(tmp_path)
    _quest_branch(repo, "quest/q1009-orders", "nine.txt")
    _quest_branch(repo, "quest/q1013-marketing", "thirteen.txt")
    _git(repo, "checkout", "quest/q1013-marketing")
    charter = repo / ".court" / "quests" / "Q1013-Test.md"
    charter.write_text(charter.read_text() + "\nquest-side note\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "charter note on the quest branch")
    _git(repo, "checkout", "castle")
    _promote(repo)
    charter.write_text(charter.read_text() + "\ntrunk-side note\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "later trunk bookkeeping")

    result = peel.peel_quest(repo, _load(repo, "Q1013"))

    assert result["bookkeeping_conflicts_kept"] == [".court/quests/Q1013-Test.md"]
    assert not (repo / "thirteen.txt").exists()
    assert (repo / "nine.txt").exists()
    assert "trunk-side note" in charter.read_text()


def test_peel_refuses_a_dirty_trunk(tmp_path):
    repo = _repo(tmp_path)
    _quest_branch(repo, "quest/q1009-orders", "nine.txt")
    _quest_branch(repo, "quest/q1013-marketing", "thirteen.txt")
    _promote(repo)
    (repo / "README.md").write_text("dirty\n")

    with pytest.raises(ValueError, match="uncommitted changes"):
        peel.peel_quest(repo, _load(repo, "Q1013"))


def test_peel_refuses_a_quest_that_was_never_promoted(tmp_path):
    repo = _repo(tmp_path)
    _quest_branch(repo, "quest/q1009-orders", "nine.txt")

    with pytest.raises(ValueError, match="not merged into castle"):
        peel.peel_quest(repo, _load(repo, "Q1013"))


def test_dry_run_touches_nothing(tmp_path):
    repo = _repo(tmp_path)
    _quest_branch(repo, "quest/q1009-orders", "nine.txt")
    _quest_branch(repo, "quest/q1013-marketing", "thirteen.txt")
    _promote(repo)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = peel.peel_quest(repo, _load(repo, "Q1013"), dry_run=True)

    assert result["dry_run"] is True
    assert (repo / "thirteen.txt").exists()
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert cg.load_peel_record(repo, "Q1013") == {}


def test_an_explicit_commit_overrides_resolution(tmp_path):
    repo = _repo(tmp_path)
    _quest_branch(repo, "quest/q1009-orders", "nine.txt")
    _quest_branch(repo, "quest/q1013-marketing", "thirteen.txt")
    _promote(repo)
    code_commit = _git(
        repo, "log", "--format=%H", "--grep", "work on quest/q1013-marketing", "castle"
    ).stdout.strip().splitlines()[0]

    result = peel.peel_quest(repo, _load(repo, "Q1013"), commit=code_commit)

    assert result["commit"] == code_commit
    assert result["mainline"] is None
    assert not (repo / "thirteen.txt").exists()
    assert (repo / "nine.txt").exists()
