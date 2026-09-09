"""Unit tests for the Court CLI Universal Targeting Convention across all verbs."""
import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from court import cli, store
from court.models import Quest


class TestUniversalTargeting(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._orig_court_dir = os.environ.get("COURT_DIR")
        os.environ["COURT_DIR"] = str(self.tmp_path / ".court")
        self.court_root = self.tmp_path / ".court"
        for d in (self.court_root / "quests", self.court_root / "epics", self.court_root / "archive"):
            d.mkdir(parents=True, exist_ok=True)

        self.parser = cli.build_parser()

        # Seed sample quests
        self.q1 = Quest(
            id="Q101-Marketing-Campaign-Analytics",
            title="Marketing Campaign Analytics",
            kind="quest",
            app="marketing",
            status="TRIBUTE_READY",
            body_sections={
                "Tribute Rendered": (
                    "## Ballad\nFixed campaign analytics query bottleneck.\n\n"
                    "## Tribute\nCreated analytics service.\n\n"
                    "## Tally\nRun query on dashboard.\n"
                )
            },
        )
        self.q2 = Quest(
            id="Q102-Platform-Task-Orchestration",
            title="Platform Task Orchestration",
            kind="quest",
            app="platform",
            status="READY_TO_RAZE",
            body_sections={
                "Tribute Rendered": (
                    "## Ballad\nReplaced cron polling with qstash.\n\n"
                    "## Tribute\nAdded dispatcher.\n\n"
                    "## Tally\nVerify qstash dashboard.\n"
                )
            },
        )
        store.save(self.q1, court_root=self.court_root, auto_commit=False)
        store.save(self.q2, court_root=self.court_root, auto_commit=False)

    def tearDown(self):
        if self._orig_court_dir:
            os.environ["COURT_DIR"] = self._orig_court_dir
        else:
            os.environ.pop("COURT_DIR", None)
        self._tmp.cleanup()

    def test_rollup_single_id_positional_after_flags(self):
        args = self.parser.parse_args(["rollup", "--section", "ballad", "Q101"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_rollup(args)
        out = buf.getvalue()
        self.assertIn("Q101-Marketing-Campaign-Analytics", out)
        self.assertIn("Fixed campaign analytics query bottleneck.", out)
        self.assertNotIn("Q102", out)

    def test_rollup_single_id_positional_before_flags(self):
        args = self.parser.parse_args(["rollup", "Q101", "--section", "ballad"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_rollup(args)
        out = buf.getvalue()
        self.assertIn("Q101-Marketing-Campaign-Analytics", out)
        self.assertIn("Fixed campaign analytics query bottleneck.", out)

    def test_rollup_comma_separated_ids(self):
        args = self.parser.parse_args(["rollup", "--section", "ballad", "Q101,Q102"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_rollup(args)
        out = buf.getvalue()
        self.assertIn("Q101-Marketing-Campaign-Analytics", out)
        self.assertIn("Q102-Platform-Task-Orchestration", out)

    def test_tally_single_id(self):
        args = self.parser.parse_args(["tally", "Q101"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_tally(args)
        out = buf.getvalue()
        self.assertIn("Q101-Marketing-Campaign-Analytics", out)
        self.assertIn("Run query on dashboard.", out)
        self.assertNotIn("Q102", out)

    def test_show_with_section(self):
        args = self.parser.parse_args(["show", "Q101", "--section", "ballad"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_show(args)
        out = buf.getvalue()
        self.assertIn("BALLAD", out)
        self.assertIn("Fixed campaign analytics query bottleneck.", out)
        self.assertNotIn("## Expected Tribute", out)

    def test_list_single_id(self):
        args = self.parser.parse_args(["list", "Q101"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_list(args)
        out = buf.getvalue()
        self.assertIn("Q101-Marketing-Campaign-Analytics", out)
        self.assertNotIn("Q102", out)

    def test_status_single_id(self):
        args = self.parser.parse_args(["status", "Q101"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_status(args)
        out = buf.getvalue()
        self.assertIn("Q101-Marketing-Campaign-Analytics", out)
        self.assertNotIn("Q102-Platform-Task-Orchestration", out)

    def test_audit_single_id(self):
        args = self.parser.parse_args(["audit", "Q101"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_audit(args)
        out = buf.getvalue()
        self.assertIn("Q101-Marketing-Campaign-Analytics", out)
        self.assertNotIn("Q102", out)

    def test_ship_single_id(self):
        args = self.parser.parse_args(["ship", "Q102"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_ship(args)
        out = buf.getvalue()
        self.assertIn("Q102-Platform-Task-Orchestration", out)
        self.assertNotIn("Q101", out)

    def test_ship_excludes_already_merged_to_main(self):
        from unittest.mock import patch
        # When Q102 is already merged to main, bare `ship` should exclude it
        with patch("court.git_ops.is_quest_merged_into", return_value=True):
            args = self.parser.parse_args(["ship"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_ship(args)
            out = buf.getvalue()
            self.assertIn("0 Quests", out)
            self.assertNotIn("Q102-Platform-Task-Orchestration", out)

    def test_ship_includes_unmerged_quest(self):
        from unittest.mock import patch
        # When Q102 is staged on castle and not yet merged into main
        with patch("court.git_ops.is_quest_merged_into", return_value=False):
            args = self.parser.parse_args(["ship"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_ship(args)
            out = buf.getvalue()
            self.assertIn("1 Quests", out)
            self.assertIn("Q102-Platform-Task-Orchestration", out)

    def test_status_cogships_ready_filters_already_merged(self):
        from unittest.mock import patch
        # When already merged to main, Cogships Ready does not show it
        with patch("court.git_ops.is_quest_merged_into", return_value=True):
            args = self.parser.parse_args(["status"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_status(args)
            out = buf.getvalue()
            self.assertNotIn("🚢 Cogships Ready", out)

        # When NOT yet merged to main, Cogships Ready displays it
        with patch("court.git_ops.is_quest_merged_into", return_value=False):
            args = self.parser.parse_args(["status"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_status(args)
            out = buf.getvalue()
            self.assertIn("🚢 Cogships Ready (1) launch with /ship", out)
            self.assertIn("Q102-Platform-Task-Orchestration", out)


if __name__ == "__main__":
    unittest.main()
