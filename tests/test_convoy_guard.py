"""Tests for the convoy promotion guard (court.convoy_guard).

These build a throwaway git repo that reproduces the cogship-382 shape:
a pack of one Quest, a second Quest branch merged onto the convoy afterwards
(for a UI preview), and a suite proof stamped before that merge.
"""
import json
import subprocess

from court import convoy_guard as cg


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "guard@test")
    _git(repo, "config", "user.name", "guard")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "castle")
    (repo / ".court" / "quests").mkdir(parents=True)
    return repo


def _charter(repo, qid, status, ui_review=""):
    body = f"---\nid: {qid}\nstatus: {status}\n---\n\n# {qid}\n"
    if ui_review:
        body += f"\n## Master of Coin's Audit\n\n- **UI Review:** {ui_review}\n"
    (repo / ".court" / "quests" / f"{qid}-Test.md").write_text(body)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"charter {qid}")


def _quest_branch(repo, branch):
    _git(repo, "checkout", "-b", branch, "castle")
    name = branch.replace("/", "-")
    (repo / f"{name}.txt").write_text("work\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"work on {branch}")
    _git(repo, "checkout", "castle")


def _convoy(repo, cogship="cogship-382"):
    branch = cg.convoy_branch(cogship)
    _git(repo, "checkout", "-b", branch, "castle")
    return branch


def _merge_into(repo, source, convoy, checkout="castle"):
    _git(repo, "checkout", convoy)
    _git(repo, "merge", "--no-ff", source, "-m", f"Merge branch '{source}' into {convoy}")
    _git(repo, "checkout", checkout)


def _suite_proof(repo, cogship, head, exit_code=0):
    directory = repo / ".court" / "suites"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{cg.normalize_cogship_id(cogship)}.json").write_text(
        json.dumps({"cogship_id": cogship, "head_sha": head, "exit_code": exit_code})
    )


def test_a_clean_pack_passes(tmp_path):
    repo = _repo(tmp_path)
    _charter(repo, "Q1009", "GATE")
    _quest_branch(repo, "quest/q1009-orders-fulfillment")
    convoy = _convoy(repo)
    _merge_into(repo, "quest/q1009-orders-fulfillment", convoy)
    cg.record_pack(repo, "cogship-382", ["Q1009-Orders-Fulfillment"])
    _suite_proof(repo, "cogship-382", cg.convoy_tip(repo, convoy))

    findings = cg.audit_promotion(repo, "cogship-382", require_suite=True)
    assert [f for f in findings if f.level == "error"] == []


def test_a_quest_merged_after_the_pack_is_a_silent_rider(tmp_path):
    repo = _repo(tmp_path)
    _charter(repo, "Q1009", "GATE")
    _charter(repo, "Q1013", "GATE", ui_review="PENDING")
    _quest_branch(repo, "quest/q1009-orders-fulfillment")
    _quest_branch(repo, "quest/q1013-marketing-email")
    convoy = _convoy(repo)
    _merge_into(repo, "quest/q1009-orders-fulfillment", convoy)
    cg.record_pack(repo, "cogship-382", ["Q1009-Orders-Fulfillment"])
    _suite_proof(repo, "cogship-382", cg.convoy_tip(repo, convoy))
    # The artist preview merges a Quest the pack never authorised.
    _merge_into(repo, "quest/q1013-marketing-email", convoy)
    cg.record_pack(repo, "cogship-382", ["Q1009-Orders-Fulfillment"])

    findings = cg.audit_promotion(repo, "cogship-382")
    errors = {f.quest for f in findings if f.level == "error"}
    assert "Q1013" in errors
    rider = [f for f in findings if f.quest == "Q1013" and "not part of the" in f.message]
    assert rider, [f.message for f in findings]


def test_a_stale_suite_proof_blocks_promotion(tmp_path):
    repo = _repo(tmp_path)
    _charter(repo, "Q1009", "GATE")
    _quest_branch(repo, "quest/q1009-orders-fulfillment")
    convoy = _convoy(repo)
    _merge_into(repo, "quest/q1009-orders-fulfillment", convoy)
    cg.record_pack(repo, "cogship-382", ["Q1009-Orders-Fulfillment"])
    _suite_proof(repo, "cogship-382", "0" * 40)

    findings = cg.audit_promotion(repo, "cogship-382", require_suite=True)
    assert any("stale" in f.message for f in findings if f.level == "error")

    allowed = cg.audit_promotion(repo, "cogship-382", allow_stale_suite=True)
    assert [f for f in allowed if f.level == "error"] == []


def test_a_pending_ui_review_blocks_promotion_unless_overridden(tmp_path):
    repo = _repo(tmp_path)
    _charter(repo, "Q1009", "GATE", ui_review="PENDING — await the Court Artist")
    _quest_branch(repo, "quest/q1009-orders-fulfillment")
    convoy = _convoy(repo)
    _merge_into(repo, "quest/q1009-orders-fulfillment", convoy)
    cg.record_pack(repo, "cogship-382", ["Q1009-Orders-Fulfillment"])

    findings = cg.audit_promotion(repo, "cogship-382")
    assert any("pending UI review" in f.message for f in findings if f.level == "error")

    overridden = cg.audit_promotion(repo, "cogship-382", allow_ui_pending=True)
    assert [f for f in overridden if f.level == "error"] == []


def test_a_non_promotable_status_blocks_promotion(tmp_path):
    repo = _repo(tmp_path)
    _charter(repo, "Q1009", "HELD")
    _quest_branch(repo, "quest/q1009-orders-fulfillment")
    convoy = _convoy(repo)
    _merge_into(repo, "quest/q1009-orders-fulfillment", convoy)
    cg.record_pack(repo, "cogship-382", ["Q1009-Orders-Fulfillment"])

    findings = cg.audit_promotion(repo, "cogship-382")
    assert any("is HELD" in f.message for f in findings if f.level == "error")


def test_the_missing_branch_degrades_to_a_warning(tmp_path):
    repo = _repo(tmp_path)
    findings = cg.audit_promotion(repo, "cogship-999")
    assert [f.level for f in findings] == ["warn"]


def test_riders_in_trunk_flags_an_unmanifested_quest(tmp_path):
    repo = _repo(tmp_path)
    _charter(repo, "Q1009", "GATE")
    _charter(repo, "Q1013", "GATE")
    _quest_branch(repo, "quest/q1009-orders-fulfillment")
    _quest_branch(repo, "quest/q1013-marketing-email")
    convoy = _convoy(repo)
    _merge_into(repo, "quest/q1009-orders-fulfillment", convoy)
    _merge_into(repo, "quest/q1013-marketing-email", convoy)
    _git(repo, "checkout", "castle")
    _git(repo, "merge", "--no-ff", convoy, "-m", f"Merge {convoy} into castle")

    findings = cg.riders_in_trunk(repo, "cogship-382", trunk="castle",
                                  manifest_quest_ids={"Q1009-Orders-Fulfillment"})
    assert [f.quest for f in findings] == ["Q1013"]
    assert findings[0].level == "error"


def test_quest_id_parsing_uses_the_last_branch_segment():
    assert cg.quest_id_from_branch("quest/q1046-intelligence-observed") == "Q1046"
    assert cg.quest_id_from_branch("quest/q1025/q1059-purchases-supplier") == "Q1059"
    assert cg.quest_id_from_branch("scout/q1079-marketing-stack") == "Q1079"
    assert cg.quest_id_from_branch("castle") == ""


def test_a_rider_merged_with_a_custom_subject_is_still_detected(tmp_path):
    repo = _repo(tmp_path)
    _charter(repo, "Q1009", "GATE")
    _charter(repo, "Q1013", "GATE")
    _quest_branch(repo, "quest/q1009-orders-fulfillment")
    _quest_branch(repo, "quest/q1013-marketing-email")
    convoy = _convoy(repo)
    _merge_into(repo, "quest/q1009-orders-fulfillment", convoy)
    cg.record_pack(repo, "cogship-382", ["Q1009-Orders-Fulfillment"])
    # An artist merge whose message hides the source branch: subject parsing
    # cannot see it, branch containment must.
    _git(repo, "checkout", convoy)
    _git(repo, "merge", "--no-ff", "quest/q1013-marketing-email", "-m", "preview the email UI")
    _git(repo, "checkout", "castle")

    findings = cg.audit_promotion(repo, "cogship-382")
    assert "Q1013" in {f.quest for f in findings if f.level == "error"}


def test_a_non_pending_ui_review_does_not_block(tmp_path):
    repo = _repo(tmp_path)
    _charter(repo, "Q1009", "GATE", ui_review="Not required — no templates touched")
    assert cg.ui_review_pending(repo, "Q1009") is False
