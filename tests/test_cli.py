import os
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

    # 4. advance
    main(["advance", "Q001-Core-Jwt-Rotation", "WORKING", "--note", "Serf dispatched"])
    out = capsys.readouterr().out
    assert "Q001-Core-Jwt-Rotation: WORKING" in out

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

### 3. Penance
Skipped testing Redis cluster failover edge case.

### 4. Audience
None required.

### 5. Humble Opinion
Recommend adding automatic key deprecation cron.
"""
    main(["set-section", "Q001-Core-Jwt-Rotation", "Tribute Rendered", "--content", tribute_body])
    capsys.readouterr()

    main(["rollup", "--section", "ballad"])
    out = capsys.readouterr().out
    assert "COURT ROLLUP — BALLAD" in out
    assert "We refactored JWT rotation" in out

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
