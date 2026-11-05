import argparse
import json
import subprocess
from pathlib import Path

import pytest

from court import cli, proofs, store


def _init_git_repo(repo_dir: Path, branch: str = "castle") -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo_dir)], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
    (repo_dir / "README.md").write_text("test repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "initial"], check=True)


def _commit(repo_dir: Path, name: str, text: str = "change") -> None:
    (repo_dir / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", name], check=True)


def _write_charter(tmp_path: Path, tribute: str, quest_id: str = "Q001-Proof-Test") -> Path:
    court_dir = tmp_path / ".court"
    (court_dir / "quests").mkdir(parents=True, exist_ok=True)
    charter = f"""---
id: {quest_id}
title: Proof test quest
kind: quest
app: core
concern: proof-test
parent_epic:
section: Feature
status: TRIBUTE_READY
branch:
worktree:
created_at: 2026-01-01T00:00:00Z
updated_at: 2026-01-01T00:00:00Z
---

# {quest_id} — Proof test quest

## Expected Tribute
{tribute}

## Tribute Rendered
(pending)
"""
    path = court_dir / "quests" / f"{quest_id}.md"
    path.write_text(charter, encoding="utf-8")
    return path


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    _init_git_repo(tmp_path, branch="castle")
    monkeypatch.setenv("COURT_DIR", str(tmp_path / ".court"))
    monkeypatch.chdir(tmp_path)
    _write_charter(
        tmp_path,
        "- [ ] First item: unit tests pass\n"
        "- [ ] Second item: migration graph check is clean\n"
        "- [ ] Third item: docs updated\n"
        "- [ ] Fourth item: cancelled work\n",
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "charter"], check=True)
    return tmp_path


def _quest():
    return store.load("Q001-Proof-Test")


def test_record_passing_and_failing_commands(repo):
    quest = _quest()
    ok = proofs.record(quest, "1", "echo hello")
    assert ok["ok"] is True
    assert ok["exit_code"] == 0
    assert len(ok["output_sha256"]) == 64
    assert "hello" in ok["output_tail"]
    assert ok["head_sha"]
    assert ok["tree_dirty"] is False
    assert ok["branch"] == "castle"

    bad = proofs.record(quest, "2", "echo boom >&2; exit 3")
    assert bad["ok"] is False
    assert bad["exit_code"] == 3

    stored = proofs.load(repo, "Q001-Proof-Test")
    assert stored["version"] == 1
    assert len(stored["records"]) == 2
    assert stored["records"][0]["output_sha256"] != stored["records"][1]["output_sha256"]


def test_load_missing_and_corrupt_file_is_empty(repo):
    assert proofs.load(repo, "Q001-Proof-Test")["records"] == []
    path = proofs.proof_path(repo, "Q001-Proof-Test")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert proofs.load(repo, "Q001-Proof-Test")["records"] == []


def test_matching_by_index_prefix_and_exact_text(repo):
    quest = _quest()
    proofs.record(quest, "1", "true")
    proofs.record(quest, "First item", "true")
    proofs.record(quest, "Second item: migration graph check is clean", "true")

    report = proofs.coverage(quest)
    by_index = {item["index"]: item for item in report["items"]}
    assert by_index[1]["proofs"] == 2 and by_index[1]["proven"] is True
    assert by_index[2]["proofs"] == 1 and by_index[2]["proven"] is True
    assert by_index[3]["proofs"] == 0 and by_index[3]["proven"] is False


def test_short_prefix_does_not_match(repo):
    quest = _quest()
    proofs.record(quest, "First", "true")  # 5 chars, below the 8-char floor
    report = proofs.coverage(quest)
    assert report["items"][0]["proofs"] == 0


def test_coverage_false_until_every_checked_item_proven(repo):
    _write_charter(
        repo,
        "- [x] First item: unit tests pass\n"
        "- [x] Second item: migration graph check is clean\n"
        "- [x] Third item: docs updated\n"
        "- [x] Fourth item: cancelled work\n",
    )
    quest = _quest()
    proofs.record(quest, "1", "true")
    proofs.record(quest, "2", "false")
    report = proofs.coverage(quest)
    assert report["ok"] is False
    assert [i["index"] for i in report["checked_unproven"]] == [2, 3, 4]

    proofs.record(quest, "2", "true")
    proofs.record(quest, "3", "true")
    proofs.record(quest, "4", "true")
    assert proofs.coverage(quest)["ok"] is True


def test_checked_item_requires_marking_checkbox_but_unchecked_not_required(repo):
    _write_charter(
        repo,
        "- [x] First item: unit tests pass\n"
        "- [ ] Second item: migration graph check is clean\n",
    )
    quest = _quest()
    report = proofs.coverage(quest)
    assert report["ok"] is False
    assert [i["index"] for i in report["checked_unproven"]] == [1]
    assert [i["index"] for i in report["unchecked"]] == [2]

    proofs.record(quest, "1", "true")
    assert proofs.coverage(quest)["ok"] is True


def test_cancelled_items_are_not_required(repo):
    _write_charter(
        repo,
        "- [x] First item: unit tests pass\n"
        "- [-] Fourth item: cancelled work\n",
    )
    quest = _quest()
    proofs.record(quest, "1", "true")
    report = proofs.coverage(quest)
    assert report["items"][1]["cancelled"] is True
    assert report["ok"] is True


def test_verify_fails_on_a_wholly_unticked_checklist(repo):
    result = proofs.verify(_quest())
    assert result["ok"] is False
    assert result.get("vacuous") is True
    assert "checked" in result["reason"]


def test_verify_allows_an_all_cancelled_checklist(repo):
    _write_charter(repo, "- [-] First item: shelved\n- [-] Second item: shelved\n")
    assert proofs.verify(_quest())["ok"] is True


def test_stale_head_detection_and_verify_reporting(repo):
    _write_charter(repo, "- [x] First item: unit tests pass\n")
    quest = _quest()
    proofs.record(quest, "1", "true")

    assert proofs.records_for(repo, quest.id, fresh_only=True) != []
    _commit(repo, "later.txt")

    assert proofs.records_for(repo, quest.id, fresh_only=True) == []
    assert len(proofs.records_for(repo, quest.id, fresh_only=False)) == 1

    result = proofs.verify(quest)
    assert result["ok"] is False
    assert [i["index"] for i in result["stale"]] == [1]
    assert [i["index"] for i in result["missing"]] == []
    assert [i["index"] for i in result["checked_unproven"]] == [1]
    assert "stale" in result["reason"]


def test_verify_reports_missing_separately_from_stale(repo):
    _write_charter(
        repo,
        "- [x] First item: unit tests pass\n"
        "- [x] Second item: migration graph check is clean\n",
    )
    quest = _quest()
    proofs.record(quest, "1", "true")
    _commit(repo, "later.txt")

    result = proofs.verify(quest)
    assert [i["index"] for i in result["stale"]] == [1]
    assert [i["index"] for i in result["missing"]] == [2]


def test_short_quest_id_resolves_to_charter_stem(repo):
    path = proofs.proof_path(repo, "Q001")
    assert path.name == "Q001-Proof-Test.json"
    assert path == repo / ".court" / "proofs" / "Q001-Proof-Test.json"


def test_unresolved_quest_raises(repo):
    with pytest.raises(proofs.ProofError):
        proofs.proof_path(repo, "Q999-Nope")


def test_format_report_lists_every_item(repo):
    _write_charter(
        repo,
        "- [x] First item: unit tests pass\n"
        "- [x] Second item: migration graph check is clean\n",
    )
    quest = _quest()
    proofs.record(quest, "1", "true")
    text = proofs.format_report(proofs.coverage(quest))
    assert "[x] 1." in text
    assert "[!] 2." in text
    assert "RESULT: FAIL" in text


def test_cmd_verify_tribute_exits_1_then_0(repo, capsys):
    _write_charter(repo, "- [x] First item: unit tests pass\n")
    quest = _quest()

    args = argparse.Namespace(quest_id=quest.id, json=False)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_verify_tribute(args)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "UNPROVEN item 1" in out

    proofs.record(quest, "1", "true")
    cli.cmd_verify_tribute(args)  # no SystemExit == exit 0
    out = capsys.readouterr().out
    assert "RESULT: ok" in out


def test_cmd_verify_tribute_json(repo, capsys):
    _write_charter(repo, "- [x] First item: unit tests pass\n")
    quest = _quest()
    args = argparse.Namespace(quest_id=quest.id, json=True)
    with pytest.raises(SystemExit):
        cli.cmd_verify_tribute(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["checked_unproven"][0]["index"] == 1


def test_cmd_prove_records_and_exits_nonzero_on_failure(repo, capsys):
    args = argparse.Namespace(
        quest_id="Q001-Proof-Test", item="1", timeout=60,
        command=["--", "true"],
    )
    cli.cmd_prove(args)
    out = capsys.readouterr().out
    assert "exit_code:   0" in out
    assert "proof:" in out

    args.command = ["--", "sh", "-c", "exit 4"]
    with pytest.raises(SystemExit) as exc:
        cli.cmd_prove(args)
    assert exc.value.code == 1
    records = proofs.load(repo, "Q001-Proof-Test")["records"]
    assert records[-1]["exit_code"] == 4


def test_cmd_prove_rejects_empty_command(repo):
    args = argparse.Namespace(quest_id="Q001-Proof-Test", item="1", timeout=60, command=[])
    with pytest.raises(SystemExit) as exc:
        cli.cmd_prove(args)
    assert exc.value.code == 2


def test_cmd_prove_rejects_unknown_item(repo):
    args = argparse.Namespace(
        quest_id="Q001-Proof-Test", item="nothing like this", timeout=60,
        command=["--", "true"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.cmd_prove(args)
    assert exc.value.code == 2


def test_cmd_proofs_is_report_only(repo, capsys):
    _write_charter(repo, "- [x] First item: unit tests pass\n")
    args = argparse.Namespace(quest_id="Q001-Proof-Test", json=True)
    assert cli.cmd_proofs(args) is None
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
