"""Unit tests for Charter Integrity & Anti-Tampering protocol."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from court import git_ops, ward
from court.models import Quest


class TestCharterIntegrity(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

        self.base_quest_text = """---
id: Q999-Common-Test-Feature
title: Test Feature
kind: quest
app: common
concern: test-feature
status: WORKING
branch: quest/q999-common-test-feature
---

# Q999-Common-Test-Feature — Test Feature

## The Kingdom Requires

Build the inventory synchronization engine without touching legacy views.

## Expected Tribute

- [ ] Implement InventorySyncService in apps/inventory/services/sync.py
- [ ] Add unit tests in apps/inventory/tests/test_sync.py
- [ ] Ensure zero N+1 database queries

## Tribute Rendered
"""

    def tearDown(self):
        self._tmp.cleanup()

    @patch("court.git_ops._run")
    def test_unmodified_charter_is_not_tampered(self, mock_run):
        mock_run.return_value = {"ok": True, "stdout": self.base_quest_text, "exit_code": 0}

        head_quest = Quest.from_markdown(self.base_quest_text)
        res = git_ops.check_charter_integrity(head_quest, self.tmp_path, base="castle")
        self.assertFalse(res["tampered"])
        self.assertEqual(len(res["violations"]), 0)

    @patch("court.git_ops._run")
    def test_checked_checkboxes_are_not_tampered(self, mock_run):
        mock_run.return_value = {"ok": True, "stdout": self.base_quest_text, "exit_code": 0}

        # Worker checked off items [x] without altering item text
        head_text = self.base_quest_text.replace("- [ ]", "- [x]")
        head_quest = Quest.from_markdown(head_text)

        res = git_ops.check_charter_integrity(head_quest, self.tmp_path, base="castle")
        self.assertFalse(res["tampered"])
        self.assertEqual(len(res["violations"]), 0)

    @patch("court.git_ops._run")
    def test_tampered_goal_and_scope_is_flagged(self, mock_run):
        mock_run.return_value = {"ok": True, "stdout": self.base_quest_text, "exit_code": 0}

        # Worker edited The Kingdom Requires to smuggle in extra scope
        head_text = self.base_quest_text.replace(
            "Build the inventory synchronization engine without touching legacy views.",
            "Build the inventory synchronization engine, rewrite legacy category_views.py, and add unchartered UI.",
        )
        head_quest = Quest.from_markdown(head_text)

        res = git_ops.check_charter_integrity(head_quest, self.tmp_path, base="castle")
        self.assertTrue(res["tampered"])
        self.assertTrue(any("The Kingdom Requires" in v for v in res["violations"]))

    @patch("court.git_ops._run")
    def test_tampered_expected_tribute_items_flagged(self, mock_run):
        mock_run.return_value = {"ok": True, "stdout": self.base_quest_text, "exit_code": 0}

        # Worker added an extra expected tribute item to justify smuggled code
        head_text = self.base_quest_text.replace(
            "- [ ] Ensure zero N+1 database queries",
            "- [x] Ensure zero N+1 database queries\n- [x] Smuggled unrequested UI component",
        )
        head_quest = Quest.from_markdown(head_text)

        res = git_ops.check_charter_integrity(head_quest, self.tmp_path, base="castle")
        self.assertTrue(res["tampered"])
        self.assertTrue(any("Expected Tribute" in v for v in res["violations"]))

    @patch("court.git_ops._run")
    def test_reworded_expected_tribute_items_flagged(self, mock_run):
        mock_run.return_value = {"ok": True, "stdout": self.base_quest_text, "exit_code": 0}

        # Worker reworded an item to match altered implementation
        head_text = self.base_quest_text.replace(
            "- [ ] Implement InventorySyncService in apps/inventory/services/sync.py",
            "- [x] Implement InventorySyncService in apps/inventory/services/sync.py and re-export in shops",
        )
        head_quest = Quest.from_markdown(head_text)

        res = git_ops.check_charter_integrity(head_quest, self.tmp_path, base="castle")
        self.assertTrue(res["tampered"])
        self.assertTrue(any("Expected Tribute" in v for v in res["violations"]))


if __name__ == "__main__":
    unittest.main()
