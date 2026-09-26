"""Unit tests for the Royal UI Atelier (`court atelier`) — batched UI review convoys."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from court import store
from court.cli import _pack_quest_branches, cmd_atelier, main
from court.models import Quest


def _make_ui_quest(qid, app="web", concern="ui", pending=True):
    audit = (
        "### Master of Coin Audit\n- **Verdict**: PASS\n"
        + (
            "- **UI Review:** PENDING (recommend Court Artist session)\n"
            if pending
            else "- **UI Review:** APPROVED by M'Lord via Court Artist\n"
        )
    )
    return Quest(
        id=qid,
        title=f"UI work {qid}",
        kind="quest",
        app=app,
        concern=concern,
        status="TRIBUTE_READY",
        branch=f"quest/{qid.lower()}-{concern}",
        section="Feature",
        body_sections={
            "The Kingdom Requires": "Make the UI pretty.",
            "Expected Tribute": "- [ ] Templates polished",
            "Master of Coin's Audit": audit,
        },
    )


class TestAtelierGating(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.court_dir = self.tmp_path / ".court"
        (self.court_dir / "quests").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    @patch("court.cli.find_kilo_binary", return_value=None)
    @patch("court.cli._pack_quest_branches")
    @patch("court.cli._verify_branch_exists", return_value=True)
    @patch("court.ward.audit_quest")
    @patch("court.cli.git_ops._run")
    @patch("court.cli.git_ops.get_repo_root")
    @patch("court.store.get_court_root")
    def test_atelier_refuses_unaudited_and_non_pending(
        self, mock_court_root, mock_repo_root, mock_git_run, mock_audit, mock_branch, mock_pack, mock_kilo
    ):
        """Only UI-review-PENDING, MoC-audited quests are accepted by the atelier."""
        mock_court_root.return_value = self.court_dir
        mock_repo_root.return_value = self.tmp_path
        mock_audit.return_value = MagicMock(violations=[], git_status={"dirty": False})
        mock_git_run.return_value = {"ok": True, "stdout": "abc123", "stderr": ""}

        unaudited = _make_ui_quest("Q501-Web-Ui", pending=True)
        unaudited.body_sections["Master of Coin's Audit"] = ""
        approved = _make_ui_quest("Q502-Web-Ui", pending=False)
        pending = _make_ui_quest("Q503-Web-Ui", pending=True)
        for q in (unaudited, approved, pending):
            store.save(q, court_root=self.court_dir, auto_commit=False)
        mock_pack.return_value = ([pending], [])

        with patch("court.store.stamp_cogship", wraps=store.stamp_cogship) as mock_stamp:
            rc = main(["atelier", "Q501-Web-Ui,Q502-Web-Ui,Q503-Web-Ui", "--port", "8123", "--no-server", "--no-commit"])
            self.assertIn(rc, (0, None))
            self.assertTrue(mock_stamp.called)
            stamped_ids = [q.id for q in mock_stamp.call_args[0][0]]
            self.assertEqual(stamped_ids, ["Q503-Web-Ui"])
            packed_ids = [q.id for q in mock_pack.call_args[0][0]]
            self.assertEqual(packed_ids, ["Q503-Web-Ui"])

    @patch("court.cli._verify_branch_exists", return_value=True)
    @patch("court.ward.audit_quest")
    @patch("court.store.get_court_root")
    def test_atelier_no_candidates_no_stamp(self, mock_court_root, mock_audit, mock_branch):
        """When every candidate is filtered out, nothing is stamped or created."""
        mock_court_root.return_value = self.court_dir
        mock_audit.return_value = MagicMock(violations=[], git_status={"dirty": False})

        headless = _make_ui_quest("Q504-Core-Batch", app="core", concern="batch", pending=False)
        store.save(headless, court_root=self.court_dir, auto_commit=False)

        with patch("court.store.stamp_cogship") as mock_stamp:
            rc = main(["atelier", "Q504-Core-Batch", "--no-commit"])
            self.assertIn(rc, (0, None))
            self.assertFalse(mock_stamp.called)


class TestAtelierConvoyStandup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.court_dir = self.tmp_path / ".court"
        (self.court_dir / "quests").mkdir(parents=True)
        self.q1 = _make_ui_quest("Q601-Web-Dash")
        self.q2 = _make_ui_quest("Q602-Web-Nav")
        for q in (self.q1, self.q2):
            store.save(q, court_root=self.court_dir, auto_commit=False)

    def tearDown(self):
        self._tmp.cleanup()

    @patch("court.cli.find_kilo_binary", return_value=None)
    @patch("court.cli._pack_quest_branches")
    @patch("court.cli._verify_branch_exists", return_value=True)
    @patch("court.migration_guard.scan_branch_contraband", return_value=[])
    @patch("court.ward.audit_quest")
    @patch("court.cli.git_ops._run")
    @patch("court.cli.git_ops.get_repo_root")
    @patch("court.store.get_court_root")
    def test_atelier_packs_merges_and_renders_convoy_prompt(
        self, mock_court_root, mock_repo_root, mock_git_run, mock_audit,
        mock_scan, mock_branch, mock_pack, mock_kilo,
    ):
        """Happy path: stamp, advance to GATE, cut convoy worktree, render prompt, record artist model."""
        mock_court_root.return_value = self.court_dir
        mock_repo_root.return_value = self.tmp_path
        mock_audit.return_value = MagicMock(violations=[], git_status={"dirty": False})
        # worktree add + ff-merge calls all succeed
        mock_git_run.return_value = {"ok": True, "stdout": "abc123", "stderr": ""}
        mock_pack.return_value = ([self.q1, self.q2], [])

        rc = main([
            "atelier", "Q601-Web-Dash,Q602-Web-Nav",
            "--port", "8123", "--no-server", "--json", "--no-commit",
        ])
        self.assertIn(rc, (0, None))

        for qid in ("Q601-Web-Dash", "Q602-Web-Nav"):
            q = store.load(qid, court_root=self.court_dir)
            self.assertEqual(q.status, "GATE")
            self.assertTrue(q.cogship_id.startswith("cogship-"))
            self.assertEqual(q.artist_model, "openrouter/z-ai/glm-5.3")
            ledger = q.body_sections.get("Castle Ledger", "")
            self.assertIn("Royal Atelier", ledger)
            self.assertIn("runserver port 8123", ledger)

        # Convoy worktree creation was attempted via git with the canonical branch name
        git_calls = [c[0][0] for c in mock_git_run.call_args_list]
        wt_add = [c for c in git_calls if c[:2] == ["git", "worktree"]]
        self.assertTrue(wt_add, "expected a git worktree add call")
        self.assertTrue(any(el.startswith("the-gatehouse/") for el in wt_add[0]))
        self.assertIn("-b", wt_add[0])

        # cmd_atelier's prompt/JSON rendering is what the caller sees; exercise the
        # template path directly through prompt-only mode to assert protocol content.
        with patch("court.cli.find_kilo_binary", return_value=None), \
             patch("court.cli._pack_quest_branches", return_value=([self.q1, self.q2], [])), \
             patch("court.cli._verify_branch_exists", return_value=True), \
             patch("court.migration_guard.scan_branch_contraband", return_value=[]), \
             patch("court.ward.audit_quest", return_value=MagicMock(violations=[], git_status={"dirty": False})), \
             patch("court.cli.git_ops._run", return_value={"ok": True, "stdout": "abc123", "stderr": ""}), \
             patch("court.cli.git_ops.get_repo_root", return_value=self.tmp_path), \
             patch("court.store.get_court_root", return_value=self.court_dir):
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                main(["atelier", "Q601-Web-Dash", "--port", "8123", "--no-server", "--prompt-only", "--no-commit"])
            prompt = buf.getvalue()
        self.assertIn("Addendum-Quests:", prompt)
        self.assertIn("Royal Addendum", prompt)
        self.assertIn("Single-Writer", prompt)
        self.assertIn("Q601-Web-Dash", prompt)

    @patch("court.cli.find_kilo_binary", return_value=None)
    @patch("court.cli._pack_quest_branches")
    @patch("court.cli._verify_branch_exists", return_value=True)
    @patch("court.migration_guard.scan_branch_contraband", return_value=[])
    @patch("court.ward.audit_quest")
    @patch("court.cli.git_ops._run")
    @patch("court.cli.git_ops.get_repo_root")
    @patch("court.store.get_court_root")
    def test_atelier_records_isolated_quests(
        self, mock_court_root, mock_repo_root, mock_git_run, mock_audit,
        mock_scan, mock_branch, mock_pack, mock_kilo,
    ):
        """A branch that conflicts with the convoy merge is isolated but stays stamped."""
        mock_court_root.return_value = self.court_dir
        mock_repo_root.return_value = self.tmp_path
        mock_audit.return_value = MagicMock(violations=[], git_status={"dirty": False})
        mock_git_run.return_value = {"ok": True, "stdout": "abc123", "stderr": ""}
        mock_pack.return_value = ([self.q1], [(self.q2.id, "CONFLICT (content): Merge conflict")])

        with patch("court.store.stamp_cogship", wraps=store.stamp_cogship) as mock_stamp:
            rc = main(["atelier", "Q601-Web-Dash,Q602-Web-Nav", "--port", "8123", "--no-server", "--no-commit"])
            self.assertIn(rc, (0, None))
            self.assertTrue(mock_stamp.called)

        q2 = store.load("Q602-Web-Nav", court_root=self.court_dir)
        self.assertEqual(q2.status, "GATE")
        ledger = q2.body_sections.get("Castle Ledger", "")
        self.assertIn("isolated", ledger.lower())


class TestPackQuestBranches(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.q1 = _make_ui_quest("Q701-Web-Dash")
        self.q2 = _make_ui_quest("Q702-Web-Nav")

    def tearDown(self):
        self._tmp.cleanup()

    @patch("court.cli.git_ops._run")
    def test_pack_isolates_failing_merge_and_restores_tree(self, mock_run):
        """Second branch conflicts: merge aborted + reset issued, quest reported isolated."""
        def _run(cmd, cwd=None):
            if cmd[1] == "merge" and len(cmd) > 2 and cmd[2] == self.q2.branch:
                return {"ok": False, "stdout": "", "stderr": "CONFLICT (content): Merge conflict in templates/web/nav.html"}
            return {"ok": True, "stdout": "", "stderr": ""}

        mock_run.side_effect = _run
        merged, isolated = _pack_quest_branches([self.q1, self.q2], self.tmp_path, "cogship-999")
        self.assertEqual([q.id for q in merged], ["Q701-Web-Dash"])
        self.assertEqual([qid for qid, _ in isolated], ["Q702-Web-Nav"])
        self.assertIn("CONFLICT", isolated[0][1])
        called_cmds = [c[0][0] for c in mock_run.call_args_list]
        self.assertIn(["git", "merge", "--abort"], called_cmds)
        self.assertIn(["git", "reset", "--hard", "HEAD"], called_cmds)


class TestAtelierTemplates(unittest.TestCase):
    def test_convoy_artist_template_carries_all_three_protocol_rules(self):
        """Paperwork attribution, isolation coupling, and single-writer are baked into the prompt."""
        tpl = (Path(__file__).resolve().parent.parent / "court" / "templates" / "court_artist_convoy_prompt.md").read_text(encoding="utf-8")
        self.assertIn("Addendum-Quests:", tpl)
        self.assertIn("## Royal Addendum", tpl)
        self.assertIn("Single-Writer", tpl)
        self.assertIn("set-section", tpl)
        self.assertIn("Zero Roleplay Jargon Leakage", tpl)

    def test_gatekeeper_template_covers_atelier_convoys(self):
        """Gatekeeper knows about pre-merged atelier convoys: single-writer pre-check + polish revert on reject."""
        tpl = (Path(__file__).resolve().parent.parent / "court" / "templates" / "gatekeeper_review_prompt.md").read_text(encoding="utf-8")
        self.assertIn("Atelier Convoys", tpl)
        self.assertIn("Single-Writer Pre-Check", tpl)
        self.assertIn("Addendum-Quests", tpl)
        self.assertIn("Addendum Commits Reverted", tpl)

    def test_atelier_command_doc_exists_in_both_locations(self):
        base = Path(__file__).resolve().parent.parent
        for p in (base / "court" / "commands" / "atelier.md", base / ".kilo" / "commands" / "atelier.md"):
            self.assertTrue(p.is_file(), f"missing {p}")
            content = p.read_text(encoding="utf-8")
            self.assertIn("court.cli atelier", content)
            self.assertIn("Single-writer", content)

    def test_cmd_atelier_registered(self):
        self.assertTrue(callable(cmd_atelier))


if __name__ == "__main__":
    unittest.main()
