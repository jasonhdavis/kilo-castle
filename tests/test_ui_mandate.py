"""Tests for UI MANDATE handling: a required artist session must block packing.

Background (Q1135, cogship-517): a royal decree made a Court Artist / UI Studio
session a mandatory step, the pipeline treats outstanding UI review as tracked
debt by design, and the Quest was packed and promoted with no artist session at
all. A mandate is not debt; these tests pin that distinction.
"""
import subprocess
from pathlib import Path

import pytest

from court import cli, convoy_guard as cg, nightwatch as nw


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".court" / "quests").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _charter(repo: Path, qid: str, status: str = "GATE", **kw) -> Path:
    audit = kw.pop("audit", "")
    notes = kw.pop("notes", "")
    body = (
        f"---\nid: {qid}\ntitle: {qid} Test\nstatus: {status}\n"
        f"branch: quest/{qid.lower()}\n---\n\n# {qid} Test\n\n## The Kingdom Requires\n\nwork\n"
    )
    if notes:
        body += f"\n## Charter Notes\n\n{notes}\n"
    if audit:
        body += f"\n## Master of Coin's Audit\n\n{audit}\n"
    p = repo / ".court" / "quests" / f"{qid}.md"
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_explicit_ui_mandate_line_is_honoured(tmp_path):
    repo = _repo(tmp_path)
    _charter(repo, "Q1", audit="- **UI Mandate:** REQUIRED — decree-2026-11-05-q1-enqueue-ui-studio")
    mandated, source = cg.ui_mandate(repo, "Q1")
    assert mandated is True
    assert "decree-2026-11-05-q1-enqueue-ui-studio" in source


def test_explicit_none_clears_the_mandate(tmp_path):
    repo = _repo(tmp_path)
    _charter(repo, "Q1", audit="- **UI Mandate:** none — cleared by M'Lord")
    assert cg.ui_mandate(repo, "Q1") == (False, "none — cleared by M'Lord")


def test_decree_sentence_in_the_charter_is_detected(tmp_path):
    """Q1135's actual wording: the ruling text pickup copies into the charter."""
    repo = _repo(tmp_path)
    _charter(
        repo, "Q1135", status="TRIBUTE_READY",
        notes=(
            "MANDATORY EXECUTION STEP: a Court Artist / UI Studio session on the rendered "
            "Push-to-Faire dialog (layout and the declared-price percentage slider "
            "interaction) with the session id and visual review evidence recorded before "
            "the UI work is called complete."
        ),
    )
    mandated, source = cg.ui_mandate(repo, "Q1135")
    assert mandated is True
    assert "UI Studio" in source


def test_explicitly_requires_phrasing_is_detected(tmp_path):
    repo = _repo(tmp_path)
    _charter(
        repo, "Q2",
        notes="M'Lord explicitly requires a UI Studio/Court Artist session for this dialog.",
    )
    assert cg.ui_mandate(repo, "Q2")[0] is True


def test_a_plain_mention_is_not_a_mandate(tmp_path):
    repo = _repo(tmp_path)
    _charter(
        repo, "Q3",
        notes="The dialog was reviewed informally; a Court Artist session was discussed but "
              "no decree requires it.",
    )
    assert cg.ui_mandate(repo, "Q3") == (False, "")


def test_no_charter_is_not_a_mandate(tmp_path):
    assert cg.ui_mandate(_repo(tmp_path), "Q404") == (False, "")


# ---------------------------------------------------------------------------
# Satisfaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("review,satisfied", [
    ("PENDING", False),
    ("DEFERRED — tracked debt", False),          # deferral must not launder a mandate
    ("APPROVED", False),                          # asserted without a sign-off line
    ('APPROVED — signed off by M\'Lord on 2026-11-10T19:00:00Z\n- **UI Sign-off:** M\'Lord — 2026-11-10T19:00:00Z', True),
    ("WAIVED by M'Lord", True),
    ("None required — M'Lord", True),
])
def test_mandate_is_outstanding_until_written_assent(tmp_path, review, satisfied):
    repo = _repo(tmp_path)
    _charter(repo, "Q4", audit=f"- **UI Mandate:** REQUIRED — decree-x\n- **UI Review:** {review}")
    outstanding, detail = cg.ui_mandate_outstanding(repo, "Q4")
    assert outstanding is (not satisfied)
    if not satisfied:
        assert "mandate:" in detail


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _git_repo(tmp_path: Path) -> Path:
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


def test_convoy_guard_errors_on_an_unmet_mandate_even_when_ui_pending_is_allowed(tmp_path):
    """--allow-ui-pending means ordinary debt is fine; it must not silence a mandate."""
    repo = _git_repo(tmp_path)
    qid, branch = "Q1135", "quest/q1135-test"
    _charter(repo, "Q1135", status="GATE",
             audit="- **UI Mandate:** REQUIRED — decree-x\n- **UI Review:** PENDING")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "charter")
    _git(repo, "checkout", "-b", branch, "castle")
    (repo / "work.txt").write_text("work\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "work")
    _git(repo, "checkout", "castle")
    convoy = cg.convoy_branch("cogship-900")
    _git(repo, "checkout", "-b", convoy, "castle")
    _git(repo, "merge", "--no-ff", branch, "-m", f"Merge branch '{branch}' into {convoy}")
    _git(repo, "checkout", "castle")

    findings = cg.audit_promotion(
        repo, "cogship-900", base="castle", expected_quest_ids={qid},
        allow_ui_pending=True,
    )
    mandate = [f for f in findings if getattr(f, "code", "") == "ui-mandate"]
    assert mandate, [f.render() for f in findings]
    assert mandate[0].level == "error"


def test_watch_holds_a_mandated_gate_quest_without_packing(tmp_path):
    snap = {"quests": [{
        "id": "Q1135-Test", "kind": "quest", "status": "GATE",
        "worktree": str(tmp_path), "worker": {"state": "idle"},
        "git_status": {"behind": 0, "behind_code": 0, "dirty": False},
        "gatekeeper_session_id": "", "cogship_id": "", "pending_audience": False,
    }]}
    policy = nw.Policy(max_workers=3, max_spawns_per_tick=2, max_spawns_per_hour=12)
    actions, notes = nw.plan(
        snap, nw.load_state(tmp_path), policy,
        {"count": 0, "rss_mb": 0.0, "by_role": {}, "workers": [], "dirs": {}},
        ui_mandate_ids=["Q1135-Test"],
    )
    assert [a for a in actions if a.kind == "collect"] == []
    human = [n for n in notes if n["level"] == "human"]
    assert any("MANDATE" in n["text"] for n in human)


# ---------------------------------------------------------------------------
# Recording / deferral refusal
# ---------------------------------------------------------------------------


def test_ui_mandate_command_records_and_clears(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    _charter(repo, "Q5-Test", status="GATE")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "charter")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli.git_ops, "get_repo_root", lambda: repo)

    cli.cmd_ui_mandate(simple_args(quest_id="Q5-Test", source="decree-q5-studio", by="steward",
                                   clear=False, no_commit=True))
    assert cg.ui_mandate(repo, "Q5-Test")[0] is True

    cli.cmd_ui_mandate(simple_args(quest_id="Q5-Test", source="owner released it", by="steward",
                                   clear=True, no_commit=True))
    assert cg.ui_mandate(repo, "Q5-Test")[0] is False


def test_deferral_cannot_launder_a_mandate(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    _charter(repo, "Q6-Test", status="GATE",
             audit="- **UI Mandate:** REQUIRED — decree-x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "charter")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli.git_ops, "get_repo_root", lambda: repo)

    with pytest.raises(SystemExit):
        cli.cmd_ui_signoff(simple_args(quest_id="Q6-Test", by="steward", note="later",
                                       defer=True, waive=False, none_required=False,
                                       no_commit=True))
    assert cg.ui_mandate_outstanding(repo, "Q6-Test")[0] is True


class simple_args:
    """Minimal argparse stand-in for command functions."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_raze_refuses_an_unmet_mandate_and_keeps_the_worktree(tmp_path, monkeypatch, capsys):
    """Razing destroys the worktree the mandated artist session needs."""
    repo = _git_repo(tmp_path)
    _charter(repo, "Q7-Test", status="READY_TO_RAZE",
             audit="- **UI Mandate:** REQUIRED — decree-x\n- **UI Review:** PENDING")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "charter")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COURT_DIR", str(repo / ".court"))
    monkeypatch.setattr(cli.git_ops, "get_repo_root", lambda: repo)

    cli.cmd_raze(simple_args(quest_id="Q7-Test", quest_ids=None, status=None, app=None,
                             epic=None, cogship=None, include_archived=False,
                             any_status=False, keep_worktree=False, no_drop_db=False,
                             force=False, dry_run=False, no_commit=False))
    out = capsys.readouterr().out
    assert "UI MANDATE unmet" in out
    from court import store
    assert store.load("Q7-Test", court_root=repo / ".court").status == "READY_TO_RAZE"
