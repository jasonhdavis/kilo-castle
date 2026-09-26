import os
import subprocess
from pathlib import Path
from court.cli import main
from court import store


def _init_git_repo(repo_dir: Path, branch: str = "castle") -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo_dir)], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
    (repo_dir / "README.md").write_text("test repo\n", encoding="utf-8")
    (repo_dir / ".gitignore").write_text(".kilo/\n.court/worktrees/\n__pycache__/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "initial"], check=True)


def test_patchwork_create_and_dispatch(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path, branch="castle")
    monkeypatch.setenv("COURT_DIR", str(tmp_path / ".court"))
    monkeypatch.chdir(tmp_path)

    # 1. init court
    main(["init"])
    capsys.readouterr()

    # 2. patchwork create & dispatch
    main([
        "patchwork",
        "--app", "api",
        "--concern", "rate-limit-leak",
        "--title", "Fix Redis rate limiter memory leak",
        "--goal", "Close redis connection and release pipeline on client abort",
        "--test-cmd", "pytest tests/test_limiter.py",
        "--native",
        "--no-run",
    ])
    out = capsys.readouterr().out
    assert "⚡ Hotfix Q001-Api-Rate-Limit-Leak chartered" in out
    assert "Dispatched Q001-Api-Rate-Limit-Leak" in out

    # 3. inspect persisted Quest record
    quest = store.load("Q001-Api-Rate-Limit-Leak", court_root=tmp_path / ".court")
    assert quest.id == "Q001-Api-Rate-Limit-Leak"
    assert quest.title == "Fix Redis rate limiter memory leak"
    assert quest.status == "WORKING"
    assert quest.section == "Bug fix"
    assert quest.is_hotfix is True
    assert "hotfix" in quest.tags
    assert quest.branch == "quest/q001-api-rate-limit-leak"
    assert quest.body_sections["The Kingdom Requires"] == "Close redis connection and release pipeline on client abort"
    assert "pytest tests/test_limiter.py" in quest.body_sections["Expected Tribute"]
    assert "<= 100 lines diff" in quest.body_sections["Expected Tribute"]
    assert quest.worktree is not None
    assert Path(quest.worktree).is_dir()


def test_patchwork_status_and_verify(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path, branch="castle")
    monkeypatch.setenv("COURT_DIR", str(tmp_path / ".court"))
    monkeypatch.chdir(tmp_path)

    main(["init"])
    capsys.readouterr()

    main([
        "patchwork",
        "--app", "core",
        "--concern", "jwt-null",
        "--title", "Fix null pointer in JWT decoder",
        "--native",
        "--no-run",
    ])
    capsys.readouterr()

    quest_id = "Q001-Core-Jwt-Null"

    # Inspect status output
    main(["patchwork", quest_id])
    status_out = capsys.readouterr().out
    assert f"⚡ Hotfix Quest: {quest_id}" in status_out
    assert "Status: [WORKING]" in status_out
    assert "Available fast-track actions:" in status_out

    # In-tree verification
    main(["patchwork", quest_id, "--verify", "--test-cmd", "python3 -c \"print('verification ok')\""])
    verify_out = capsys.readouterr().out
    assert "Running hotfix verification" in verify_out
    assert "In-tree verification PASSED" in verify_out
    assert "verification ok" in verify_out


def test_patchwork_collect_and_promote_inline(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path, branch="castle")
    monkeypatch.setenv("COURT_DIR", str(tmp_path / ".court"))
    monkeypatch.chdir(tmp_path)

    main(["init"])
    capsys.readouterr()

    main([
        "patchwork",
        "--app", "auth",
        "--concern", "session-expiry",
        "--title", "Fix session expiry timestamp format",
        "--native",
        "--no-run",
    ])
    capsys.readouterr()

    quest_id = "Q001-Auth-Session-Expiry"
    quest = store.load(quest_id, court_root=tmp_path / ".court")
    wt = Path(quest.worktree)

    # Make a small fix in the worktree
    (wt / "session.py").write_text("SESSION_TIMEOUT = 3600\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "session.py"], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-m", "fix(auth): update session timeout format"], check=True)

    # Add Tribute Rendered
    tribute_rendered = """### 1. Ballad
Fixed session expiry timestamp format.

### 2. Tribute
- session.py: updated timeout format

### 3. Tally
- Verified session duration test.

### 4. Penance
None.

### 5. Audience
None required.

### 6. Humble Opinion
Ready to ship.
"""
    quest.set_section("Tribute Rendered", tribute_rendered)
    store.save(quest, court_root=tmp_path / ".court", auto_commit=False)

    # 1. Collect inline
    main(["patchwork", quest_id, "--collect"])
    collect_out = capsys.readouterr().out
    assert "Inline Master of Coin audit recorded" in collect_out
    assert "Advanced Q001-Auth-Session-Expiry to GATE in cogship-" in collect_out

    quest_after_collect = store.load(quest_id, court_root=tmp_path / ".court")
    assert quest_after_collect.status == "GATE"
    assert "Master of Coin Audit (Hotfix Fast-Track)" in quest_after_collect.body_sections["Master of Coin's Audit"]

    # 2. Promote inline
    main(["patchwork", quest_id, "--promote", "--test-cmd", "python3 -c \"print('tests green')\""])
    promote_out = capsys.readouterr().out
    assert "Running gate verification suite for hotfix" in promote_out
    assert "Promoting hotfix Q001-Auth-Session-Expiry" in promote_out
    assert "successfully promoted to castle and marked READY_TO_RAZE" in promote_out

    quest_after_promote = store.load(quest_id, court_root=tmp_path / ".court")
    assert quest_after_promote.status == "READY_TO_RAZE"


def test_standard_collect_auto_audits_hotfix(tmp_path, monkeypatch, capsys):
    """Test that standard `court collect` or `court collect --hotfix` auto-audits hotfix quests."""
    _init_git_repo(tmp_path, branch="castle")
    monkeypatch.setenv("COURT_DIR", str(tmp_path / ".court"))
    monkeypatch.chdir(tmp_path)

    main(["init"])
    capsys.readouterr()

    main([
        "patchwork",
        "--app", "billing",
        "--concern", "stripe-webhook",
        "--title", "Fix stripe webhook signature check",
        "--native",
        "--no-run",
    ])
    capsys.readouterr()

    quest_id = "Q001-Billing-Stripe-Webhook"
    quest = store.load(quest_id, court_root=tmp_path / ".court")
    wt = Path(quest.worktree)

    (wt / "webhook.py").write_text("VERIFY_SIG = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "webhook.py"], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-m", "fix(billing): verify stripe webhook signature"], check=True)

    # Set Tribute Rendered
    quest.set_section("Tribute Rendered", "### 1. Ballad\nFixed signature check.\n\n### 2. Tribute\n- webhook.py\n\n### 3. Tally\n- test ok\n\n### 4. Penance\nNone\n\n### 5. Audience\nNone\n\n### 6. Humble Opinion\nShip\n")
    store.save(quest, court_root=tmp_path / ".court", auto_commit=False)

    # Advance to TRIBUTE_READY without manual Master of Coin audit
    main(["advance", quest_id, "TRIBUTE_READY", "--note", "Tribute rendered"])
    capsys.readouterr()

    # Run standard collect - should recognize hotfix tag and perform inline audit
    main(["collect", quest_id])
    collect_out = capsys.readouterr().out
    assert "Stamped 1 Quest(s) onto cogship-" in collect_out
    assert f"{quest_id}: GATE" in collect_out

    updated = store.load(quest_id, court_root=tmp_path / ".court")
    assert updated.status == "GATE"
    assert "Master of Coin Audit (Hotfix Fast-Track)" in updated.body_sections["Master of Coin's Audit"]


def test_patchwork_invalid_branch_name(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path, branch="castle")
    monkeypatch.setenv("COURT_DIR", str(tmp_path / ".court"))
    monkeypatch.chdir(tmp_path)

    main(["init"])
    capsys.readouterr()

    import pytest
    with pytest.raises(SystemExit):
        main([
            "patchwork",
            "--app", "core",
            "--concern", "flat-branch",
            "--title", "Invalid Branch Test",
            "--branch", "flat-branch-name-no-slash",
            "--native",
            "--no-run",
        ])
    err = capsys.readouterr().err
    assert "must use organizational folder prefix" in err or "violates folder hierarchy" in err


def test_patchwork_missing_args(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path, branch="castle")
    monkeypatch.setenv("COURT_DIR", str(tmp_path / ".court"))
    monkeypatch.chdir(tmp_path)

    main(["init"])
    capsys.readouterr()

    import pytest
    with pytest.raises(SystemExit):
        main(["patchwork"])
    err = capsys.readouterr().err
    assert "ERROR: Provide either (--app, --concern, --title) to create a hotfix" in err
