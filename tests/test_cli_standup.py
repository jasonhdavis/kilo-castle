"""Unit tests for mechanized CLI standup, agent definitions, and pure task prompts."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from court import store
from court.cli import (
    build_serf_task_prompt,
    cmd_charter,
    cmd_dispatch,
    find_kilo_binary,
    main,
    setup_worktree_agent_config,
    standup_kilo_session,
)
from court.models import Quest


class TestCliStandup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.court_dir = self.tmp_path / ".court"
        self.quests_dir = self.court_dir / "quests"
        self.quests_dir.mkdir(parents=True)

        self.quest = Quest(
            id="Q123-Core-Standup",
            title="Implement Mechanized Standup",
            kind="quest",
            app="core",
            concern="standup",
            status="OPEN",
            branch="quest/q123-core-standup",
            section="Feature",
            body_sections={
                "The Kingdom Requires": "Build CLI standup mechanism without prompt corruption.",
                "Expected Tribute": "- [ ] Add agent definitions\n- [ ] Mechanize court dispatch",
            },
        )
        store.save(self.quest, court_root=self.court_dir, auto_commit=False)

    def tearDown(self):
        self._tmp.cleanup()

    def test_build_serf_task_prompt_pure_instructions(self):
        """Verify prompt contains ONLY pure task instructions, no persona boilerplate."""
        prompt = build_serf_task_prompt(self.quest)

        # Must include task requirements
        self.assertIn("# Quest Q123-Core-Standup: Implement Mechanized Standup", prompt)
        self.assertIn("Branch: quest/q123-core-standup", prompt)
        self.assertIn("Section: Feature", prompt)
        self.assertIn("Build CLI standup mechanism without prompt corruption.", prompt)
        self.assertIn("- [ ] Add agent definitions", prompt)

        # Must NOT include persona boilerplate
        self.assertNotIn("You are a Serf (never a Steward)", prompt)
        self.assertNotIn("ZERO ROLEPLAY LEAKAGE", prompt)
        self.assertNotIn("STRICT CHARTER IMMUTABILITY", prompt)
        self.assertNotIn("IMMEDIATE MANDATORY FIRST STEP", prompt)
        self.assertNotIn("Report to the King / Bear Tribute structure", prompt)
        self.assertNotIn("Scout Predecessor", prompt)

    def test_build_serf_task_prompt_with_scout_predecessor(self):
        """Verify Scout Predecessor block is included when scout_of is set."""
        self.quest.scout_of = "Q120-Core-Scout-Spike"
        prompt = build_serf_task_prompt(self.quest)
        self.assertIn("## Scout Predecessor", prompt)
        self.assertIn("Q120-Core-Scout-Spike", prompt)

    def test_setup_worktree_agent_config(self):
        """Verify .kilo/kilo.json is written with default_agent: serf."""
        wt_dir = self.tmp_path / "worktree_test"
        wt_dir.mkdir(parents=True)

        setup_worktree_agent_config(wt_dir, "serf")

        kilo_cfg_path = wt_dir / ".kilo" / "kilo.json"
        self.assertTrue(kilo_cfg_path.is_file())
        cfg = json.loads(kilo_cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg.get("default_agent"), "serf")

        # Test updating an existing config preserves existing keys
        kilo_cfg_path.write_text(json.dumps({"custom_setting": True}), encoding="utf-8")
        setup_worktree_agent_config(wt_dir, "serf")
        cfg_updated = json.loads(kilo_cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg_updated.get("default_agent"), "serf")
        self.assertTrue(cfg_updated.get("custom_setting"))

    def test_find_kilo_binary_env_var(self):
        """Verify KILO_BIN environment variable override works."""
        fake_bin = self.tmp_path / "fake_kilo"
        fake_bin.write_text("#!/bin/sh\necho kilo", encoding="utf-8")
        fake_bin.chmod(0o755)

        with patch.dict(os.environ, {"KILO_BIN": str(fake_bin)}):
            found = find_kilo_binary()
            self.assertEqual(found, fake_bin)

    def test_standup_kilo_session_config_mode(self):
        """Verify standup_kilo_session writes .kilo/TASK.md and configures worktree when run_now=False."""
        wt_dir = self.tmp_path / "worktree_session"
        wt_dir.mkdir(parents=True)

        res = standup_kilo_session(
            worktree_path=wt_dir,
            agent="serf",
            model="openrouter/z-ai/glm-5.3-flash",
            prompt="Do task",
            title="Q123 Serf Worker",
            run_now=False,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["session_id"], "kilo-serf")
        self.assertTrue((wt_dir / ".kilo" / "TASK.md").is_file())
        self.assertEqual((wt_dir / ".kilo" / "TASK.md").read_text(encoding="utf-8"), "Do task")

    @patch("court.cli.subprocess.Popen")
    def test_standup_kilo_session_cli_mode(self, mock_popen):
        """Verify standup_kilo_session launches background Kilo CLI with proper agent and model."""
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc

        wt_dir = self.tmp_path / "worktree_cli"
        wt_dir.mkdir(parents=True)
        fake_bin = self.tmp_path / "kilo"
        fake_bin.write_text("#!/bin/sh\nexit 0", encoding="utf-8")
        fake_bin.chmod(0o755)

        with patch("court.cli.query_latest_kilo_session_id", return_value="ses_test123"):
            res = standup_kilo_session(
                worktree_path=wt_dir,
                agent="serf",
                model="GLM-5.3-Flash",
                prompt="Do work",
                title="Q123 Serf Worker",
                kilo_bin=fake_bin,
                run_now=True,
            )

        self.assertTrue(res["ok"])
        self.assertEqual(res["mode"], "cli")
        self.assertEqual(res["session_id"], "ses_test123")
        self.assertTrue(mock_popen.called)
        cmd_called = mock_popen.call_args[0][0]
        self.assertIn("--agent", cmd_called)
        self.assertIn("serf", cmd_called)
        self.assertIn("--model", cmd_called)
        self.assertIn("openrouter/z-ai/glm-5.3-flash", cmd_called)

    @patch("court.cli.find_kilo_binary", return_value=None)
    @patch("court.git_ops.create_git_worktree")
    @patch("court.git_ops.get_repo_root")
    @patch("court.store.get_court_root")
    def test_dispatch_automated_standup(self, mock_court_root, mock_repo_root, mock_create_wt, mock_find_kilo):
        """Verify court dispatch without --worktree performs automated standup and transitions to WORKING."""
        mock_court_root.return_value = self.court_dir
        mock_repo_root.return_value = self.tmp_path
        wt_path = self.tmp_path / "wt_auto"
        mock_create_wt.return_value = {"ok": True, "path": str(wt_path), "branch": "quest/q123-core-standup"}

        with patch("court.store.find_path") as mock_find:
            mock_find.return_value = self.quests_dir / "Q123-Core-Standup.md"
            rc = main(["dispatch", "Q123-Core-Standup", "--standup", "--no-commit"])

        self.assertIn(rc, (0, None))
        q = store.load("Q123-Core-Standup", court_root=self.court_dir)
        self.assertEqual(q.status, "WORKING")
        self.assertIn("wt_auto", q.worktree)
        self.assertTrue(mock_create_wt.called)
        # Check worktree config was created
        self.assertTrue((wt_path / ".kilo" / "kilo.json").is_file())

    @patch("court.cli.find_kilo_binary", return_value=None)
    @patch("court.git_ops.create_git_worktree")
    @patch("court.git_ops.get_repo_root")
    @patch("court.store.get_court_root")
    def test_charter_with_dispatch_flag(self, mock_court_root, mock_repo_root, mock_create_wt, mock_find_kilo):
        """Verify court charter --dispatch charters and stands up the Serf in one call."""
        mock_court_root.return_value = self.court_dir
        mock_repo_root.return_value = self.tmp_path
        wt_path = self.tmp_path / "wt_charter"
        mock_create_wt.return_value = {"ok": True, "path": str(wt_path), "branch": "quest/q123-core-standup"}

        with patch("court.store.find_path") as mock_find:
            mock_find.return_value = self.quests_dir / "Q123-Core-Standup.md"
            rc = main(["charter", "Q123-Core-Standup", "--notes", "Implement fully", "--dispatch", "--no-commit"])

        self.assertIn(rc, (0, None))
        q = store.load("Q123-Core-Standup", court_root=self.court_dir)
        self.assertEqual(q.status, "WORKING")
        self.assertIn("M'Lord's Charter Notes: Implement fully", q.body_sections["The Kingdom Requires"])

    @patch("court.cli.subprocess.Popen")
    @patch("court.store.get_court_root")
    def test_coin_dispatch_kilo_cli(self, mock_court_root, mock_popen):
        """Verify court coin dispatches Master of Coin via Kilo CLI with proper agent."""
        mock_court_root.return_value = self.court_dir
        mock_proc = MagicMock()
        mock_proc.pid = 88888
        mock_popen.return_value = mock_proc

        wt_path = self.tmp_path / "wt_coin"
        wt_path.mkdir(parents=True)
        self.quest.worktree = str(wt_path)
        self.quest.status = "TRIBUTE_READY"
        store.save(self.quest, court_root=self.court_dir, auto_commit=False)

        fake_bin = self.tmp_path / "kilo"
        fake_bin.write_text("#!/bin/sh\nexit 0", encoding="utf-8")
        fake_bin.chmod(0o755)

        with patch("court.cli.find_kilo_binary", return_value=fake_bin):
            with patch("court.cli.query_latest_kilo_session_id", return_value="ses_coin_999"):
                with patch("court.store.find_path") as mock_find:
                    mock_find.return_value = self.quests_dir / "Q123-Core-Standup.md"
                    rc = main(["coin", "Q123-Core-Standup", "--no-commit"])

        self.assertIn(rc, (0, None))
        self.assertTrue(mock_popen.called)
        cmd_called = mock_popen.call_args[0][0]
        self.assertIn("--agent", cmd_called)
        self.assertIn("master_of_coin", cmd_called)
        self.assertIn("--model", cmd_called)
        self.assertIn("openrouter/google/gemini-3.8-flash", cmd_called)
        q = store.load("Q123-Core-Standup", court_root=self.court_dir)
        self.assertEqual(q.master_of_coin_session_id, "ses_coin_999")

    @patch("court.cli.subprocess.Popen")
    @patch("court.store.get_court_root")
    def test_goad_dispatch_kilo_cli(self, mock_court_root, mock_popen):
        """Verify court goad dispatches Serf goad via Kilo CLI."""
        mock_court_root.return_value = self.court_dir
        mock_proc = MagicMock()
        mock_proc.pid = 77777
        mock_popen.return_value = mock_proc

        wt_path = self.tmp_path / "wt_goad"
        wt_path.mkdir(parents=True)
        self.quest.worktree = str(wt_path)
        self.quest.status = "WORKING"
        store.save(self.quest, court_root=self.court_dir, auto_commit=False)

        fake_bin = self.tmp_path / "kilo"
        fake_bin.write_text("#!/bin/sh\nexit 0", encoding="utf-8")
        fake_bin.chmod(0o755)

        with patch("court.cli.find_kilo_binary", return_value=fake_bin):
            with patch("court.cli.query_latest_kilo_session_id", return_value="ses_goad_777"):
                with patch("court.store.find_path") as mock_find:
                    mock_find.return_value = self.quests_dir / "Q123-Core-Standup.md"
                    rc = main(["goad", "Q123-Core-Standup", "--no-commit"])

        self.assertIn(rc, (0, None))
        self.assertTrue(mock_popen.called)
        cmd_called = mock_popen.call_args[0][0]
        self.assertIn("--agent", cmd_called)
        self.assertIn("serf", cmd_called)
        q = store.load("Q123-Core-Standup", court_root=self.court_dir)
        self.assertEqual(q.serf_session_id, "ses_goad_777")


class TestAgentDefinitions(unittest.TestCase):
    def test_court_agents_exist(self):
        """Verify all five Court agents are defined with valid YAML frontmatter."""
        pkg_agents_dir = Path(__file__).resolve().parent.parent / "court" / "agents"
        required_agents = ["serf", "scout", "gatekeeper", "master_of_coin", "steward"]

        for agent in required_agents:
            agent_file = pkg_agents_dir / f"{agent}.md"
            self.assertTrue(agent_file.is_file(), f"Missing agent definition: {agent_file}")
            content = agent_file.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("---\n"), f"Agent {agent} missing YAML frontmatter header")
            self.assertIn("mode: primary", content)
            self.assertIn("model: ", content)
            self.assertIn("description: ", content)

            if agent in ("serf", "scout", "master_of_coin"):
                self.assertIn("task: deny", content, f"Agent {agent} must enforce task: deny permission")


if __name__ == "__main__":
    unittest.main()
