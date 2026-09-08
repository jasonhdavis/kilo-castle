import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from court.models import Quest, FRONTMATTER_FIELDS
from court import cli


class TestCourtArtist(unittest.TestCase):
    def test_frontmatter_and_model_fields(self):
        """Verify artist_session_id and artist_model exist in FRONTMATTER_FIELDS and Quest dataclass."""
        self.assertIn("artist_session_id", FRONTMATTER_FIELDS)
        self.assertIn("artist_model", FRONTMATTER_FIELDS)

        q = Quest(
            id="Q901",
            title="Test Artist Quest",
            app="common",
            concern="ui-review",
            artist_session_id="ses_artist_12345",
            artist_model="GLM-5.3",
        )
        md = q.to_markdown()
        self.assertIn("artist_session_id: ses_artist_12345", md)
        self.assertIn("artist_model: GLM-5.3", md)

        loaded = Quest.from_markdown(md)
        self.assertEqual(loaded.artist_session_id, "ses_artist_12345")
        self.assertEqual(loaded.artist_model, "GLM-5.3")

    def test_extract_ui_review_status(self):
        """Verify extraction of UI Review status from Master of Coin's Audit."""
        # 1. Approved status
        q1 = Quest(
            id="Q902",
            title="Approved UI Quest",
            body_sections={
                "Master of Coin's Audit": (
                    "- **Verdict:** PASS\n"
                    "- **UI Review:** APPROVED by M'Lord via Court Artist (Session: ses_123, Port: 8012)\n"
                    "- **Commutation:** none\n"
                )
            },
        )
        self.assertEqual(
            q1.extract_ui_review_status(),
            "APPROVED by M'Lord via Court Artist (Session: ses_123, Port: 8012)",
        )

        # 2. Pending status
        q2 = Quest(
            id="Q903",
            title="Pending UI Quest",
            body_sections={
                "Master of Coin's Audit": (
                    "- **Verdict:** PASS\n"
                    "- **UI Review:** PENDING (Recommend Court Artist session via /artist Q903)\n"
                    "- **Commutation:** none\n"
                )
            },
        )
        self.assertEqual(
            q2.extract_ui_review_status(),
            "PENDING (Recommend Court Artist session via /artist Q903)",
        )

        # 3. None required
        q3 = Quest(
            id="Q904",
            title="No UI Quest",
            body_sections={
                "Master of Coin's Audit": (
                    "- **Verdict:** PASS\n"
                    "- **UI Review:** None required (headless logic only)\n"
                    "- **Commutation:** none\n"
                )
            },
        )
        self.assertEqual(
            q3.extract_ui_review_status(),
            "None required (headless logic only)",
        )

    def test_extract_target_routes(self):
        """Verify routes extraction from Tally and template paths."""
        q = Quest(
            id="Q905",
            title="Tally Route Quest",
            app="common",
            concern="products",
            body_sections={
                "Tribute Rendered": (
                    "## 3. Tally (Production & UI Verification Runbook)\n\n"
                    "1. `/products/categories/` — EXPECT: Product Categories title\n"
                    "2. `/products/all/` — EXPECT: Shopify Stores badges\n"
                    "3. Run `python scripts/test.py` against /tmp/scratch.json\n"
                )
            },
        )
        routes = cli._extract_target_routes(q)
        self.assertIn("/products/categories/", routes)
        self.assertIn("/products/all/", routes)
        # Should not extract file path /tmp/scratch.json
        self.assertNotIn("/tmp/scratch.json", routes)

    def test_ensure_worktree_server_override(self):
        """Verify port override returns given port."""
        with tempfile.TemporaryDirectory() as tmpdir:
            port, url, desc = cli._ensure_worktree_server(Path(tmpdir), port_override=9099)
            self.assertEqual(port, 9099)
            self.assertEqual(url, "http://localhost:9099")
            self.assertIn("Manual port override", desc)

    def test_ensure_worktree_server_reads_port_file(self):
        """Verify reading from existing .worktree-port file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / ".worktree-port").write_text("8123\n")
            port, url, desc = cli._ensure_worktree_server(tmp_path, no_server=True)
            self.assertEqual(port, 8123)
            self.assertEqual(url, "http://localhost:8123")

    @patch("court.git_ops.find_worktree_for_quest")
    @patch("court.store.load")
    @patch("court.store.save")
    def test_cmd_artist_json_output(self, mock_save, mock_load, mock_find_wt):
        """Test cmd_artist with --json output and model flexibility."""
        with tempfile.TemporaryDirectory() as tmpdir:
            wt_path = Path(tmpdir)
            (wt_path / ".worktree-port").write_text("8088\n")

            q = Quest(
                id="Q906",
                title="Interactive UI Review Quest",
                app="shops",
                concern="catalog",
                branch="quest/q906-shops-catalog",
                worktree=str(wt_path),
                body_sections={
                    "Tribute Rendered": "## 3. Tally\n1. `/shops/catalog/` — EXPECT: list view\n"
                },
            )
            mock_load.return_value = q
            mock_find_wt.return_value = wt_path

            args = MagicMock()
            args.quest_id = "Q906"
            args.model = "google/gemini-2.5-pro"
            args.provider = "openrouter"
            args.port = None
            args.no_server = True
            args.no_commit = True
            args.prompt_only = False
            args.json = True

            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_artist(args)

            output = buf.getvalue()
            data = json.loads(output)

            self.assertEqual(data["quest_id"], "Q906")
            self.assertEqual(data["port"], 8088)
            self.assertEqual(data["runserver_url"], "http://localhost:8088")
            self.assertEqual(data["model"], "google/gemini-2.5-pro")
            self.assertEqual(data["task"]["model"], "google/gemini-2.5-pro")
            self.assertEqual(data["task"]["name"], "Q906 Court Artist")
            self.assertIn("/shops/catalog/", data["routes"])
            self.assertIn("Court Artist", data["prompt"])
            self.assertIn("http://localhost:8088", data["prompt"])

            # Verify quest model updated
            self.assertEqual(q.artist_model, "google/gemini-2.5-pro")
            mock_save.assert_called_once()

    @patch("court.git_ops.find_worktree_for_quest")
    @patch("court.store.load")
    @patch("court.store.save")
    def test_cmd_artist_default_model_uses_glm53(self, mock_save, mock_load, mock_find_wt):
        """Test cmd_artist defaults to GLM-5.3 when no model override is provided."""
        with tempfile.TemporaryDirectory() as tmpdir:
            wt_path = Path(tmpdir)
            (wt_path / ".worktree-port").write_text("8089\n")

            q = Quest(
                id="Q906b",
                title="Default Artist Model Quest",
                app="shops",
                concern="catalog",
                branch="quest/q906b-shops-catalog",
                worktree=str(wt_path),
                body_sections={
                    "Tribute Rendered": "## 3. Tally\n1. `/shops/catalog/` — EXPECT: list view\n"
                },
            )
            mock_load.return_value = q
            mock_find_wt.return_value = wt_path

            args = MagicMock()
            args.quest_id = "Q906b"
            args.model = None
            args.provider = None
            args.port = None
            args.no_server = True
            args.no_commit = True
            args.prompt_only = False
            args.json = True

            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_artist(args)

            output = buf.getvalue()
            data = json.loads(output)

            self.assertEqual(data["model"], "GLM-5.3")
            self.assertEqual(data["task"]["model"], "GLM-5.3")
            self.assertEqual(data["provider"], "openrouter")
            self.assertEqual(data["task"]["provider"], "openrouter")
            self.assertEqual(q.artist_model, "GLM-5.3")
            mock_save.assert_called_once()

    @patch("court.ward.audit_quest")
    @patch("court.store.stamp_cogship")
    @patch("court.store.save")
    @patch("court.cli.resolve_quest_selection")
    def test_cmd_collect_skips_pending_ui_review(self, mock_resolve, mock_save, mock_stamp, mock_audit):
        """Test that court collect refuses to pack a Quest whose UI Review is PENDING."""
        q = Quest(
            id="Q907",
            title="Quest with Pending UI Review",
            app="common",
            concern="ui",
            status="TRIBUTE_READY",
            body_sections={
                "Master of Coin's Audit": (
                    "- **Verdict:** PASS\n"
                    "- **UI Review:** PENDING (Recommend Court Artist session via /artist Q907)\n"
                )
            },
        )
        mock_resolve.return_value = [q]
        mock_audit_res = MagicMock()
        mock_audit_res.git_status = {"dirty": False}
        mock_audit_res.violations = []
        mock_audit.return_value = mock_audit_res

        args = MagicMock()
        args.base = "castle"
        args.no_commit = True
        args.force = False
        args.skip_ui_review = False
        args.cogship = None

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_collect(args)

        output = buf.getvalue()
        self.assertIn("UI Review is PENDING", output)
        self.assertIn("/artist Q907", output)
        mock_stamp.assert_not_called()

    @patch("court.ward.audit_quest")
    @patch("court.store.stamp_cogship")
    @patch("court.store.save")
    @patch("court.cli.resolve_quest_selection")
    def test_cmd_collect_allows_skip_ui_review(self, mock_resolve, mock_save, mock_stamp, mock_audit):
        """Test that court collect packs pending UI Quest when --skip-ui-review is passed."""
        q = Quest(
            id="Q908",
            title="Quest with Skipped UI Review",
            app="common",
            concern="ui",
            branch="quest/q908-ui",
            status="TRIBUTE_READY",
            body_sections={
                "The Kingdom Requires": "Scope",
                "Expected Tribute": "- [x] Done",
                "Tribute Rendered": (
                    "## 1. Ballad\nDone\n"
                    "## 2. Tribute\nDone\n"
                    "## 3. Tally\nDone\n"
                    "## 4. Penance\nNone\n"
                    "## 5. Audience\nNone\n"
                    "## 6. Humble Opinion\nDone\n"
                ),
                "Master of Coin's Audit": (
                    "- **Verdict:** PASS\n"
                    "- **UI Review:** PENDING (Recommend Court Artist session via /artist Q908)\n"
                )
            },
        )
        mock_resolve.return_value = [q]
        mock_audit_res = MagicMock()
        mock_audit_res.git_status = {"dirty": False}
        mock_audit_res.violations = []
        mock_audit.return_value = mock_audit_res
        mock_stamp.return_value = "cogship-999"

        args = MagicMock()
        args.base = "castle"
        args.no_commit = True
        args.force = False
        args.skip_ui_review = True
        args.cogship = "cogship-999"

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_collect(args)

        output = buf.getvalue()
        self.assertIn("Stamped 1 Quest(s) onto cogship-999", output)
        mock_stamp.assert_called_once()


if __name__ == "__main__":
    unittest.main()
