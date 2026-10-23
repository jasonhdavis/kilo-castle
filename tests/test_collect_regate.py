"""Re-gate / hand-back support for already-promoted convoys (2026-10-23).

Incident: once a convoy promoted into castle, `court collect` could not re-mint
it — the compliance audit reported "Zero commits delivered on branch" for every
promoted Quest and skipped it, reusing the promoted cogship id was impossible,
and a promoted convoy's still-fresh Gatekeeper session made `collect` think the
convoy was live. A bare `kilo run --agent gatekeeper` also died (missing
`--auto`). These tests cover the engine fixes.
"""
import io
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from court import cli, store
from court.models import Quest


def _init_git_repo(repo_dir: Path, branch: str = "castle") -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo_dir)], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "t"], check=True)
    (repo_dir / "README.md").write_text("repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "init"], check=True)


def test_promoted_convoy_is_never_live(tmp_path, monkeypatch):
    _init_git_repo(tmp_path, branch="castle")
    # Convoy branch merged into castle = promoted.
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-q", "-b", "the-gatehouse/cogship-010"], check=True)
    (tmp_path / "work.txt").write_text("work\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "convoy work"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-q", "castle"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "merge", "-q", "--no-ff", "-m", "promote", "the-gatehouse/cogship-010"], check=True)

    wt = tmp_path / ".kilo" / "worktrees" / "the-gatehouse-cogship-010"
    wt.mkdir(parents=True)

    # Even with a session updated seconds ago, a promoted convoy is not live.
    monkeypatch.setattr(cli, "session_is_fresh", lambda p, max_age_ms=0: True)
    is_live, why = cli.gatehouse_convoy_is_live("cogship-010", repo_root=tmp_path)
    assert is_live is False
    assert "promoted" in why


def test_unpromoted_convoy_with_fresh_session_is_live(tmp_path, monkeypatch):
    _init_git_repo(tmp_path, branch="castle")
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-q", "-b", "the-gatehouse/cogship-011"], check=True)
    (tmp_path / "work.txt").write_text("work\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "convoy work"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-q", "castle"], check=True)

    wt = tmp_path / ".kilo" / "worktrees" / "the-gatehouse-cogship-011"
    wt.mkdir(parents=True)
    monkeypatch.setattr(cli, "session_is_fresh", lambda p, max_age_ms=0: True)
    is_live, _why = cli.gatehouse_convoy_is_live("cogship-011", repo_root=tmp_path)
    assert is_live is True


def _regate_quest() -> Quest:
    return Quest(
        id="Q901-Marketing-Activation",
        title="Activation",
        app="marketing",
        concern="activation",
        status="READY_TO_RAZE",
        branch="quest/q901-marketing-activation",
        cogship_id="cogship-010",
        cogship_promoted_commit="deadbeef",
        gatekeeper_session_id="ses_old",
        gatekeeper_model="glm-5.3",
        body_sections={
            "The Kingdom Requires": "Do it.",
            "Expected Tribute": "- [x] Done",
            "Master of Coin's Audit": "- **Verdict:** PASS\n- **UI Review:** None required",
        },
    )


def _collect_args(**overrides) -> MagicMock:
    args = MagicMock()
    args.base = "castle"
    args.no_commit = True
    args.force = False
    args.skip_ui_review = False
    args.hotfix = False
    args.regate = False
    args.cogship = None
    args.standup = False
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def _run_collect(quest, args, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "resolve_quest_selection", lambda *a, **k: [quest])
    monkeypatch.setattr(cli.git_ops, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "require_op_lock", lambda name: None)
    monkeypatch.setattr(cli.ward, "audit_quest", lambda q, base_branch="castle": _clean_audit())
    monkeypatch.setattr(cli.migration_guard, "scan_branch_contraband", lambda *a, **k: [])
    monkeypatch.setattr(cli, "gatehouse_convoy_is_live", lambda cid, repo_root=None: (False, "dead"))
    monkeypatch.setattr(store, "save_many", lambda quests, *a, **k: None)
    (tmp_path / ".kilo" / "worktrees").mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    with patch.object(cli.store, "save"):
        with redirect_stdout(buf):
            cli.cmd_collect(args)
    return buf.getvalue()


def _clean_audit():
    a = MagicMock()
    a.git_status = {"dirty": False}
    a.violations = []
    return a


def test_collect_skips_promoted_quest_without_regate(tmp_path, monkeypatch):
    quest = _regate_quest()
    out = _run_collect(quest, _collect_args(), tmp_path, monkeypatch)
    assert "already promoted" in out
    assert "--regate" in out
    assert quest.status == "READY_TO_RAZE"


def test_collect_regate_mints_fresh_convoy_and_clears_stamps(tmp_path, monkeypatch):
    quest = _regate_quest()
    stamped = {}

    def fake_stamp(quests, cogship_id=None, auto_commit=True, force=False):
        stamped["ids"] = [q.id for q in quests]
        stamped["cogship_id"] = cogship_id
        stamped["force"] = force
        return "cogship-011"

    monkeypatch.setattr(store, "stamp_cogship", fake_stamp)
    out = _run_collect(quest, _collect_args(regate=True), tmp_path, monkeypatch)

    assert stamped == {"ids": [quest.id], "cogship_id": None, "force": True}
    assert quest.status == "GATE"
    assert quest.cogship_promoted_commit == ""
    assert quest.gatekeeper_session_id == ""
    assert quest.gatekeeper_model == ""
    assert "Stamped 1 Quest(s) onto cogship-011" in out


def test_collect_regate_refuses_to_reuse_promoted_convoy_id(tmp_path, monkeypatch):
    quest = _regate_quest()
    monkeypatch.setattr(store, "stamp_cogship", lambda *a, **k: "cogship-010")
    with_redirect = io.StringIO()
    try:
        with redirect_stdout(with_redirect):
            _run_collect(quest, _collect_args(regate=True, cogship="cogship-010"), tmp_path, monkeypatch)
    except SystemExit as e:
        assert e.code == 1
    else:
        raise AssertionError("expected SystemExit when reusing a promoted convoy id")


def test_collect_new_cogship_keyword_allocates(tmp_path, monkeypatch):
    quest = _regate_quest()
    quest.status = "TRIBUTE_READY"
    quest.cogship_id = ""
    quest.cogship_promoted_commit = ""
    captured = {}

    def fake_stamp(quests, cogship_id=None, auto_commit=True, force=False):
        captured["cogship_id"] = cogship_id
        return "cogship-012"

    monkeypatch.setattr(store, "stamp_cogship", fake_stamp)
    out = _run_collect(quest, _collect_args(regate=True, cogship="new"), tmp_path, monkeypatch)
    assert captured["cogship_id"] is None
    # Printed Gatekeeper instructions must be headless-safe: a bare
    # `kilo run --agent gatekeeper` dies on an auto-rejected permission.
    assert "kilo run --auto --agent gatekeeper" in out
