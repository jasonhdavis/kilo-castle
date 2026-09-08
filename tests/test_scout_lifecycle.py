"""
Unit tests for the automated Scout-to-Quest lifecycle transition:
when a production Quest whose frontmatter names a source Scout (`scout_of`)
advances to WORKING, that source Scout should be auto-transitioned to
READY_TO_RAZE, since its exploratory findings are already captured in its
own Scout Report and it has served its purpose.
"""
import argparse
import os
import tempfile
import unittest
from pathlib import Path

from court import store, cli
from court.models import Quest


class TestScoutToQuestLifecycleTransition(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._orig_court_dir = os.environ.get("COURT_DIR")
        os.environ["COURT_DIR"] = str(self.tmp_path / ".court")
        self.court_root = self.tmp_path / ".court"
        for d in (self.court_root / "quests", self.court_root / "epics", self.court_root / "archive"):
            d.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self._orig_court_dir:
            os.environ["COURT_DIR"] = self._orig_court_dir
        else:
            os.environ.pop("COURT_DIR", None)
        self._tmp.cleanup()

    def _make_advance_args(self, quest_id: str, status: str) -> argparse.Namespace:
        return argparse.Namespace(
            quest_ids=quest_id,
            status=status,
            force=False,
            no_commit=True,
            note="",
        )

    def test_advancing_successor_to_working_transitions_source_scout(self):
        scout = Quest(
            id="Q900-Test-Scout-Spike",
            title="Test Scout Spike",
            kind="scout",
            section="Investigation",
            status="TRIBUTE_READY",
            branch="scout/q900-test-scout-spike",
        )
        store.save(scout, court_root=self.court_root, auto_commit=False)

        successor = Quest(
            id="Q901-Test-Successor-Quest",
            title="Test Successor Quest",
            kind="quest",
            status="PLANNED",
            branch="quest/q901-test-successor-quest",
            scout_of="Q900-Test-Scout-Spike",
        )
        store.save(successor, court_root=self.court_root, auto_commit=False)

        args = self._make_advance_args("Q901-Test-Successor-Quest", "WORKING")
        cli.cmd_advance(args)

        reloaded_successor = store.load("Q901-Test-Successor-Quest", court_root=self.court_root)
        self.assertEqual(reloaded_successor.status, "WORKING")

        reloaded_scout = store.load("Q900-Test-Scout-Spike", court_root=self.court_root)
        self.assertEqual(
            reloaded_scout.status, "READY_TO_RAZE",
            "Source Scout must auto-transition to READY_TO_RAZE once its "
            "successor Quest enters WORKING.",
        )
        self.assertIn("Q901-Test-Successor-Quest", reloaded_scout.body_sections.get("Castle Ledger", ""))

    def test_no_scout_of_field_is_a_no_op(self):
        plain_quest = Quest(
            id="Q902-Test-Plain-Quest",
            title="Test Plain Quest (no Scout predecessor)",
            kind="quest",
            status="PLANNED",
            branch="quest/q902-test-plain-quest",
        )
        store.save(plain_quest, court_root=self.court_root, auto_commit=False)

        args = self._make_advance_args("Q902-Test-Plain-Quest", "WORKING")
        cli.cmd_advance(args)

        reloaded = store.load("Q902-Test-Plain-Quest", court_root=self.court_root)
        self.assertEqual(reloaded.status, "WORKING")

    def test_already_razed_scout_is_left_alone(self):
        scout = Quest(
            id="Q903-Test-Already-Razed-Scout",
            title="Test Already Razed Scout",
            kind="scout",
            section="Investigation",
            status="READY_TO_RAZE",
            branch="scout/q903-test-already-razed-scout",
        )
        store.save(scout, court_root=self.court_root, auto_commit=False)

        successor = Quest(
            id="Q904-Test-Successor-Of-Razed-Scout",
            title="Test Successor of Already Razed Scout",
            kind="quest",
            status="PLANNED",
            branch="quest/q904-test-successor-of-razed-scout",
            scout_of="Q903-Test-Already-Razed-Scout",
        )
        store.save(successor, court_root=self.court_root, auto_commit=False)

        args = self._make_advance_args("Q904-Test-Successor-Of-Razed-Scout", "WORKING")
        cli.cmd_advance(args)

        reloaded_scout = store.load("Q903-Test-Already-Razed-Scout", court_root=self.court_root)
        self.assertEqual(reloaded_scout.status, "READY_TO_RAZE")


if __name__ == "__main__":
    unittest.main()
