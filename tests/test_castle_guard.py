import unittest
from unittest.mock import patch

from court import castle_guard


class TestCastleGuard(unittest.TestCase):
    def test_classify_whitelist_paths(self):
        self.assertEqual(castle_guard.classify_path(".court/quests/Q001-Test.md"), castle_guard.WHITELIST)
        self.assertEqual(castle_guard.classify_path(".court/epics/Q010-Test.md"), castle_guard.WHITELIST)
        self.assertEqual(castle_guard.classify_path(".court/LEDGER.md"), castle_guard.WHITELIST)
        self.assertEqual(castle_guard.classify_path(".court/EDICTS.md"), castle_guard.WHITELIST)
        self.assertEqual(castle_guard.classify_path("tasks/BACKLOG.md"), castle_guard.WHITELIST)
        self.assertEqual(castle_guard.classify_path("AGENTS.md"), castle_guard.WHITELIST)

    def test_classify_blacklist_paths(self):
        self.assertEqual(castle_guard.classify_path("court/cli.py"), castle_guard.BLACKLIST)
        self.assertEqual(castle_guard.classify_path(".court/engine/cli.py"), castle_guard.BLACKLIST)
        self.assertEqual(castle_guard.classify_path("apps/common/views.py"), castle_guard.BLACKLIST)
        self.assertEqual(castle_guard.classify_path("tests/test_cli.py"), castle_guard.BLACKLIST)
        self.assertEqual(castle_guard.classify_path("kilo.json"), castle_guard.BLACKLIST)
        self.assertEqual(castle_guard.classify_path(".kilo/commands/charter.md"), castle_guard.BLACKLIST)

    def test_classify_unclassified_paths(self):
        self.assertEqual(castle_guard.classify_path("manage.py"), castle_guard.UNCLASSIFIED)
        self.assertEqual(castle_guard.classify_path("Dockerfile"), castle_guard.UNCLASSIFIED)
        self.assertEqual(castle_guard.classify_path("requirements.txt"), castle_guard.UNCLASSIFIED)

    def test_evaluate_staged_paths(self):
        # All whitelist allowed
        res = castle_guard._classify_staged([".court/quests/Q001.md", "tasks/ACTIVE.md"])
        self.assertTrue(res["allowed"])

        # Blacklist rejected
        res = castle_guard._classify_staged(["court/cli.py"])
        self.assertFalse(res["allowed"])

        # Unclassified rejected on castle
        res = castle_guard._classify_staged(["manage.py"])
        self.assertFalse(res["allowed"])


class TestPushPolicy(unittest.TestCase):
    Q118 = "refs/heads/quest/q118-integration-evasion abc123 refs/heads/castle def456"

    def test_is_local_remote_url(self):
        for url in (".", "..", "./sibling", "/abs/path", "file:///tmp/repo", ""):
            self.assertTrue(castle_guard.is_local_remote_url(url), url)
        for url in ("https://github.com/o/r.git", "git@github.com:o/r.git", "ssh://git@host/o/r"):
            self.assertFalse(castle_guard.is_local_remote_url(url), url)

    def test_local_remote_allows_everything(self):
        # Master of Coin sync-back (`git push . HEAD:<real-branch>`) from a
        # quest worktree must never be blocked.
        lines = [self.Q118]
        res = castle_guard.evaluate_pre_push(lines, ".")
        self.assertTrue(res["allowed"])

    def test_pipeline_branches_never_reach_hosted_remotes(self):
        for src in ("quest/q116-x", "scout/q116-x", "the-gatehouse/cogship-042"):
            lines = [f"refs/heads/{src} abc123 refs/heads/{src} 0000000"]
            with self.subTest(src=src):
                res = castle_guard.evaluate_pre_push(lines, "https://github.com/o/r.git")
                self.assertFalse(res["allowed"])

    def test_quest_branch_pushed_onto_hosted_castle_rejected(self):
        # cogship incident: quest branch pushed straight onto origin/castle.
        res = castle_guard.evaluate_pre_push([self.Q118], "https://github.com/o/r.git")
        self.assertFalse(res["allowed"])
        self.assertIn("quest/q118-integration-evasion -> castle", res["offenders"][0]["path"])

    def test_castle_to_hosted_castle_fast_forward_allowed(self):
        lines = ["refs/heads/castle newsha refs/heads/castle oldsha"]
        with patch.object(castle_guard, "_git", return_value=(0, "", "")) as mock_git:
            res = castle_guard.evaluate_pre_push(lines, "https://github.com/o/r.git")
        self.assertTrue(res["allowed"])
        self.assertEqual(
            mock_git.call_args[0][0],
            ["merge-base", "--is-ancestor", "oldsha", "newsha"],
        )

    def test_castle_history_rewrite_rejected(self):
        lines = ["refs/heads/castle newsha refs/heads/castle oldsha"]
        with patch.object(castle_guard, "_git", return_value=(1, "", "")):
            res = castle_guard.evaluate_pre_push(lines, "https://github.com/o/r.git")
        self.assertFalse(res["allowed"])

    def test_castle_first_creation_allowed_without_ancestry_check(self):
        lines = [f"refs/heads/castle newsha refs/heads/castle {castle_guard.ZERO_SHA}"]
        with patch.object(castle_guard, "_git", return_value=(1, "", "")) as mock_git:
            res = castle_guard.evaluate_pre_push(lines, "https://github.com/o/r.git")
        self.assertTrue(res["allowed"])
        self.assertFalse(mock_git.called)

    def test_main_push_to_hosted_remote_allowed(self):
        # court deploy pushes main to origin — must keep working.
        lines = ["refs/heads/main sha1 refs/heads/main sha2"]
        res = castle_guard.evaluate_pre_push(lines, "https://github.com/o/r.git")
        self.assertTrue(res["allowed"])

    def test_deletion_of_guarded_refs_rejected(self):
        # deletion payload: empty local ref/sha -> two whitespace tokens
        castle_del = f" refs/heads/castle {castle_guard.ZERO_SHA}"
        quest_del = f" refs/heads/quest/q116-x {castle_guard.ZERO_SHA}"
        for line in (castle_del, quest_del):
            with self.subTest(line=line):
                res = castle_guard.evaluate_pre_push([line], "https://github.com/o/r.git")
                self.assertFalse(res["allowed"])

    def test_deletion_of_main_allowed(self):
        lines = [f" refs/heads/main {castle_guard.ZERO_SHA}"]
        res = castle_guard.evaluate_pre_push(lines, "https://github.com/o/r.git")
        self.assertTrue(res["allowed"])

    def test_run_pre_push_hook_resolves_remote_url(self):
        def fake_git(args, cwd=None):
            if args[:2] == ["remote", "get-url"]:
                return (0, "https://github.com/o/r.git", "")
            return (0, "", "")

        lines = [self.Q118]
        with patch.object(castle_guard, "_git", side_effect=fake_git):
            res = castle_guard.run_pre_push_hook("origin", stdin_lines=lines)
        self.assertFalse(res["allowed"])


if __name__ == "__main__":
    unittest.main()
