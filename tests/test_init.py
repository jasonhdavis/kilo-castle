from pathlib import Path
from court.init_cmd import run_init
from court import store


def test_init_scaffolds_repository(tmp_path):
    result = run_init(target_dir=tmp_path)
    assert (tmp_path / ".court").is_dir()
    assert (tmp_path / ".court" / "quests").is_dir()
    assert (tmp_path / ".court" / "epics").is_dir()
    assert (tmp_path / ".court" / "archive").is_dir()
    assert (tmp_path / ".court" / "templates").is_dir()
    assert (tmp_path / ".court" / "README.md").is_file()
    assert (tmp_path / ".court" / "LEDGER.md").is_file()
    assert (tmp_path / ".court" / "config.json").is_file()

    # Templates copied
    assert (tmp_path / ".court" / "templates" / "serf_dispatch_prompt.md").is_file()
    assert (tmp_path / ".court" / "templates" / "master_of_coin_review_prompt.md").is_file()
    assert (tmp_path / ".court" / "templates" / "gatekeeper_review_prompt.md").is_file()
    assert (tmp_path / ".court" / "templates" / "vassal_dispatch_prompt.md").is_file()
    assert (tmp_path / ".court" / "templates" / "bear_tribute_prompt.md").is_file()

    # Kilo commands
    assert (tmp_path / ".kilo" / "commands" / "charter.md").is_file()
    assert (tmp_path / ".kilo" / "commands" / "levy.md").is_file()
    assert (tmp_path / ".kilo" / "commands" / "collect.md").is_file()
    assert (tmp_path / ".kilo" / "commands" / "raze.md").is_file()
    assert (tmp_path / ".kilo" / "commands" / "status.md").is_file()
    assert (tmp_path / ".kilo" / "commands" / "plot.md").is_file()
    assert (tmp_path / ".kilo" / "commands" / "ship.md").is_file()

    # Kilo prompts
    assert (tmp_path / ".kilo" / "prompts" / "steward.md").is_file()
    assert (tmp_path / ".kilo" / "prompts" / "master_of_coin.md").is_file()
    assert (tmp_path / ".kilo" / "prompts" / "gatekeeper.md").is_file()

    # Kilo agents
    assert (tmp_path / ".kilo" / "agents" / "serf.md").is_file()
    assert (tmp_path / ".kilo" / "agents" / "scout.md").is_file()
    assert (tmp_path / ".kilo" / "agents" / "gatekeeper.md").is_file()
    assert (tmp_path / ".kilo" / "agents" / "master_of_coin.md").is_file()
    assert (tmp_path / ".kilo" / "agents" / "steward.md").is_file()
    assert (tmp_path / ".kilo" / "agent" / "serf.md").is_file()

    # Worktree setup script
    assert (tmp_path / ".kilo" / "setup-script").is_file()

    # kilo.json configuration
    assert (tmp_path / "kilo.json").is_file()

    # AGENTS.md
    assert (tmp_path / "AGENTS.md").is_file()
