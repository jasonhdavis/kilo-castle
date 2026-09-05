import os
import subprocess
from pathlib import Path
from court.cli import main
from court import store


def test_cli_new_show_advance_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COURT_DIR", str(tmp_path / ".court"))
    monkeypatch.chdir(tmp_path)

    # 1. init
    main(["init"])
    out = capsys.readouterr().out
    assert "Kilo Castle initialization complete" in out

    # 2. new
    main([
        "new",
        "--app", "core",
        "--concern", "jwt-rotation",
        "--title", "Rotate JWT secret keys",
        "--section", "Bug fix",
        "--goal", "Fix token rotation race condition",
        "--tribute", "- [ ] Tests pass",
    ])
    out = capsys.readouterr().out
    assert "Created Q001-Core-Jwt-Rotation" in out

    # 3. show
    main(["show", "Q001-Core-Jwt-Rotation"])
    out = capsys.readouterr().out
    assert "id: Q001-Core-Jwt-Rotation" in out
    assert "Fix token rotation race condition" in out

    # 4. advance (single & batch)
    main(["advance", "Q001-Core-Jwt-Rotation", "WORKING", "--note", "Serf dispatched"])
    out = capsys.readouterr().out
    assert "Q001-Core-Jwt-Rotation: WORKING" in out

    main(["advance", "Q001-Core-Jwt-Rotation,Q001-Core-Jwt-Rotation", "REVIEW", "--note", "Batch advance test"])
    out = capsys.readouterr().out
    assert "Q001-Core-Jwt-Rotation: REVIEW" in out

    main(["advance", "Q001-Core-Jwt-Rotation", "WORKING", "--note", "Reset to working"])
    capsys.readouterr()

    # 5. set-field
    main(["set-field", "Q001-Core-Jwt-Rotation", "serf_model", "Gemini 3.7 Flash"])
    out = capsys.readouterr().out
    assert "serf_model = Gemini 3.7 Flash" in out

    # 6. set-section
    main(["set-section", "Q001-Core-Jwt-Rotation", "Master of Coin Review", "--content", "Value approved"])
    out = capsys.readouterr().out
    assert "Updated section 'Master of Coin Review'" in out

    # 7. status
    main(["status"])
    out = capsys.readouterr().out
    assert "[WORKING] (1)" in out
    assert "Q001-Core-Jwt-Rotation" in out

    # 8. list
    main(["list"])
    out = capsys.readouterr().out
    assert "Q001-Core-Jwt-Rotation" in out

    # 9. tribute rendered & rollup
    tribute_body = """### 1. Ballad
We refactored JWT rotation to eliminate the token race.

### 2. Tribute
- Added 3 test cases in tests/test_jwt.py (100% pass)

### 3. Tally
- Navigate to /api/v1/auth/token/refresh
- Pass expired access token with valid refresh token
- Expect 200 OK with new JWT token

### 4. Penance
Skipped testing Redis cluster failover edge case.

### 5. Audience
None required.

### 6. Humble Opinion
Recommend adding automatic key deprecation cron.
"""
    main(["set-section", "Q001-Core-Jwt-Rotation", "Tribute Rendered", "--content", tribute_body])
    capsys.readouterr()

    main(["rollup", "--section", "ballad"])
    out = capsys.readouterr().out
    assert "COURT ROLLUP — BALLAD" in out
    assert "We refactored JWT rotation" in out

    main(["rollup", "--section", "tally"])
    out = capsys.readouterr().out
    assert "COURT ROLLUP — TALLY" in out
    assert "Navigate to /api/v1/auth/token/refresh" in out

    main(["tally"])
    out = capsys.readouterr().out
    assert "THE COURT TALLY — PRODUCTION & UI VERIFICATION RUNBOOKS" in out
    assert "Navigate to /api/v1/auth/token/refresh" in out

    main(["rollup", "--section", "penance"])
    out = capsys.readouterr().out
    assert "Skipped testing Redis cluster failover" in out

    main(["rollup", "--section", "opinion"])
    out = capsys.readouterr().out
    assert "Recommend adding automatic key deprecation cron" in out

    # 10. edict
    main(["edict", "Focus on authentication hardening"])
    out = capsys.readouterr().out
    assert "Updated .court/EDICTS.md" in out

    main(["edict"])
    out = capsys.readouterr().out
    assert "Focus on authentication hardening" in out

    # 11. tree and status --tree
    main([
        "new",
        "--kind", "epic",
        "--app", "auth",
        "--concern", "refactor",
        "--title", "Auth Refactor Initiative",
    ])
    capsys.readouterr()

    main([
        "new",
        "--app", "auth",
        "--concern", "cleanup",
        "--title", "Auth Cleanup Task",
        "--epic", "Q002-Auth-Refactor",
    ])
    capsys.readouterr()

    main(["tree"])
    tree_out = capsys.readouterr().out
    assert "THE COURT — Quest & Epic Hierarchy" in tree_out
    assert "🏰 EPICS" in tree_out
    assert "Q002-Auth-Refactor" in tree_out
    assert "Q003-Auth-Cleanup" in tree_out
    assert "⚔️ STANDALONE QUESTS" in tree_out
    assert "Q001-Core-Jwt-Rotation" in tree_out

    main(["status", "--tree"])
    status_tree_out = capsys.readouterr().out
    assert "THE COURT — Quest & Epic Hierarchy" in status_tree_out

    main(["show", "Q002-Auth-Refactor"])
    show_epic_out = capsys.readouterr().out
    assert "# Child Quests" in show_epic_out
    assert "Q003-Auth-Cleanup" in show_epic_out

    # 12. ship (Cog Ship deployment convoy summary)
    main(["advance", "Q001-Core-Jwt-Rotation", "GATE", "--note", "test setup"])
    capsys.readouterr()
    main(["advance", "Q001-Core-Jwt-Rotation", "READY_FOR_TEARDOWN", "--note", "Merged for ship test"])
    capsys.readouterr()

    main(["ship"])
    ship_out = capsys.readouterr().out
    assert "COG SHIP DEPLOYMENT CONVOY" in ship_out
    assert "Q001-Core-Jwt-Rotation" in ship_out
    assert "THE BARD'S CHRONICLE" in ship_out
    assert "We refactored JWT rotation" in ship_out
    assert "THE COFFERS LEDGER" in ship_out
    assert "THE TALLY RUNBOOK" in ship_out
    assert "THE SERF PENANCE" in ship_out
    assert "THE HUMBLE OPINIONS" in ship_out
    assert "COG SHIP DEPLOYMENT SUMMARY COMPLETE" in ship_out


def _init_git_repo(repo_dir: Path, branch: str = "castle") -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo_dir)], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
    (repo_dir / "README.md").write_text("test repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "initial"], check=True)


def test_cli_engine_port_commands(tmp_path, monkeypatch, capsys):
    """Exercise the newly-ported engine subcommands (pillory, stamp, audit,
    rebase, diff) against a temp Court repo fixture, reusing the same
    COURT_DIR/chdir pattern as the rest of this file."""
    _init_git_repo(tmp_path, branch="castle")
    monkeypatch.setenv("COURT_DIR", str(tmp_path / ".court"))
    monkeypatch.chdir(tmp_path)

    main(["init"])
    capsys.readouterr()

    main([
        "new",
        "--app", "core",
        "--concern", "engine-port",
        "--title", "Engine Port Quest",
        "--section", "Bug fix",
    ])
    capsys.readouterr()

    quest_id = "Q001-Core-Engine-Port"

    # audit: single quest, text and JSON modes.
    main(["audit", quest_id])
    audit_out = capsys.readouterr().out
    assert f"Audit Report for {quest_id}" in audit_out

    main(["audit", quest_id, "--json"])
    audit_json_out = capsys.readouterr().out
    assert '"quest_id": "Q001-Core-Engine-Port"' in audit_json_out

    # audit: batch mode over all quests.
    main(["audit"])
    audit_all_out = capsys.readouterr().out
    assert "THE WARD — WORKTREE & TRIBUTE AUDIT" in audit_all_out

    # rebase: no worktree resolvable -> skipped, not a crash.
    main(["rebase", quest_id])
    rebase_out = capsys.readouterr().out
    assert quest_id in rebase_out
    assert "no resolvable worktree" in rebase_out or "skipped" in rebase_out

    # diff: no worktree resolvable -> clean error exit, not a crash.
    try:
        main(["diff", quest_id])
    except SystemExit as e:
        assert e.code != 0
    capsys.readouterr()

    # stamp: allocate a fresh Cog Ship id across one quest.
    main(["stamp", quest_id])
    stamp_out = capsys.readouterr().out
    assert "cogship-001" in stamp_out
    stamped = store.load(quest_id, court_root=tmp_path / ".court")
    assert stamped.cogship_id == "cogship-001"

    # pillory: no proof of landing found -> Quest is punished.
    main(["pillory", quest_id, "--reason", "Missing tests", "--decrees", "Reuse the service layer"])
    pillory_out = capsys.readouterr().out
    assert "PUNISHED" in pillory_out
    punished = store.load(quest_id, court_root=tmp_path / ".court")
    assert punished.status == "PUNISHED"
    assert "Missing tests" in punished.body_sections["Judgement of the Condemned"]

    # status dashboard should now show the PUNISHED quest.
    main(["status"])
    status_out = capsys.readouterr().out
    assert "[PUNISHED]" in status_out

    # timber: cross-references worktrees/quests; must not crash even with no
    # Agent Manager state file present.
    main(["timber"])
    timber_out = capsys.readouterr().out
    assert "PHYSICAL GIT WORKTREES" in timber_out

    # ward: compliance patrol summary; --check-fresh is optional and must
    # degrade gracefully when unconfigured.
    main(["ward", "--check-fresh"])
    ward_out = capsys.readouterr().out
    assert "THE WARD — COMPLIANCE PATROL" in ward_out
    assert "unavailable" in ward_out or "optional" in ward_out.lower()

    # fix-branches: dry-run must not crash against a repo with only
    # already-canonical branches.
    main(["fix-branches", "--dry-run"])
    fix_out = capsys.readouterr().out
    assert "BRANCH REALIGNMENT" in fix_out
