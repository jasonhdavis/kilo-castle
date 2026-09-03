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
