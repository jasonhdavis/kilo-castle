import unittest
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


if __name__ == "__main__":
    unittest.main()
