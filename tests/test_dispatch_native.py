"""Unit tests for court dispatch native worktree creation and config loading."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from court import config, git_ops, store
from court.cli import main
from court.models import Quest


class TestDispatchNative(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.court_dir = self.tmp_path / ".court"
        self.quests_dir = self.court_dir / "quests"
        self.quests_dir.mkdir(parents=True)

        self.quest = Quest(
            id="Q101-Core-Auth",
            title="Core Auth Feature",
            kind="quest",
            app="core",
            concern="auth",
            status="PLANNED",
            branch="quest/q101-core-auth",
        )
        store.save(self.quest, court_root=self.court_dir, auto_commit=False)

    def tearDown(self):
        self._tmp.cleanup()

    @patch("court.git_ops.create_git_worktree")
    @patch("court.git_ops.get_repo_root")
    @patch("court.store.get_court_root")
    def test_dispatch_create_worktree(self, mock_court_root, mock_repo_root, mock_create_wt):
        mock_court_root.return_value = self.court_dir
        mock_repo_root.return_value = self.tmp_path
        mock_create_wt.return_value = {"ok": True, "path": str(self.tmp_path / "wt"), "branch": "quest/q101-core-auth"}

        with patch("court.store.find_path") as mock_find:
            mock_find.return_value = self.quests_dir / "Q101-Core-Auth.md"
            rc = main(["dispatch", "Q101-Core-Auth", "--create-worktree", "--no-commit"])

        self.assertIn(rc, (0, None))
        q = store.load("Q101-Core-Auth", court_root=self.court_dir)
        self.assertEqual(q.status, "WORKING")
        self.assertEqual(q.serf_session_id, "native")
        self.assertTrue(mock_create_wt.called)

    def test_config_defaults_and_override(self):
        # Default config
        cfg = config.load_config(self.court_dir)
        self.assertEqual(cfg["models"]["serf"], "GLM-5.3-Flash")
        self.assertFalse(cfg["no_kilo_mode"])

        # Override via config.json
        cfg_file = self.court_dir / "config.json"
        cfg_file.write_text(json.dumps({
            "models": {
                "serf": "claude-3-5-sonnet",
                "serf_provider": "anthropic",
            },
            "no_kilo_mode": True,
        }), encoding="utf-8")

        cfg_custom = config.load_config(self.court_dir)
        self.assertEqual(cfg_custom["models"]["serf"], "claude-3-5-sonnet")
        self.assertEqual(cfg_custom["models"]["serf_provider"], "anthropic")
        self.assertTrue(cfg_custom["no_kilo_mode"])


if __name__ == "__main__":
    unittest.main()
