"""Unit tests for the Deterministic Multi-Quest Combined Studio (`court studio`, Q-2).

Covers: candidate gating, the merge conflict policy (charter paperwork ->
branch-wins; genuine code overlap -> disclosed union resolution; unresolvable
-> isolation), the freshness gate (WARN-ONLY), studio/session ledger recording,
both documented spawn paths (Branch A AM payload / Branch B CLI), and the
post-sign-off --sync-back submode.
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from court import store
from court.cli import (
    _is_paperwork_path,
    _freshness_verdict,
    _merge_quest_branches_with_policy,
    _studio_slug,
    _union_merge_path,
    main,
)
from court.models import Quest

AUDIT_KEY = "Master of Coin's Audit"
BLOB = {"1": "base", "2": "ours", "3": "theirs"}


def _ok(stdout=""):
    return {"ok": True, "stdout": stdout, "stderr": ""}


def _fail(stderr="boom"):
    return {"ok": False, "stdout": "", "stderr": stderr}


def _make_ui_quest(qid, pending=True, status="TRIBUTE_READY", branch=None, **kw):
    audit = (
        "### Master of Coin Audit\n- **Verdict**: PASS\n"
        + (
            "- **UI Review:** PENDING (recommend Court Artist session)\n"
            if pending
            else "- **UI Review:** APPROVED by M'Lord via Court Artist\n"
        )
    )
    defaults = dict(
        id=qid,
        title=f"UI work {qid}",
        kind="quest",
        app="web",
        concern="ui",
        status=status,
        branch=branch or f"quest/{qid.lower().split('-')[0]}-ui",
        section="Feature",
        body_sections={
            "The Kingdom Requires": "Make the UI pretty.",
            "Expected Tribute": "- [ ] Templates polished",
            AUDIT_KEY: audit,
        },
    )
    defaults.update(kw)
    return Quest(**defaults)


class _GitRecorder:
    """Fake git_ops._run that records commands; performs the worktree mkdir the
    real `git worktree add` would perform."""

    def __init__(self, perform_worktree_mkdir=True):
        self.calls = []
        self.perform_worktree_mkdir = perform_worktree_mkdir

    def __call__(self, cmd, cwd=None, **kw):
        self.calls.append(list(cmd))
        if self.perform_worktree_mkdir and cmd[:3] == ["git", "worktree", "add"]:
            Path(cmd[2]).mkdir(parents=True, exist_ok=True)
        return _ok()


def _verify_quest_branches_only(branch, repo_root=None):
    """Quest branches exist; a fresh studio branch (artist/...) does not yet."""
    return not branch.startswith("artist/")


class TestStudioHelpers(unittest.TestCase):
    def test_is_paperwork_path(self):
        self.assertTrue(_is_paperwork_path(".court/quests/Q617-Orders-X.md"))
        self.assertTrue(_is_paperwork_path(".court/epics/Q050.md"))
        self.assertTrue(_is_paperwork_path(".court/LEDGER.md"))
        self.assertTrue(_is_paperwork_path(".kilo/TASK_ARTIST.md"))
        self.assertTrue(_is_paperwork_path('".court/quests/Q1.md"'))  # quoted variant
        self.assertFalse(_is_paperwork_path("templates/pages/orders.html"))
        self.assertFalse(_is_paperwork_path("apps/web/views.py"))
        self.assertFalse(_is_paperwork_path(""))

    def test_studio_slug_uses_short_ids(self):
        quests = [_make_ui_quest("Q617-Orders-X"), _make_ui_quest("Q627-Web-Y")]
        self.assertEqual(_studio_slug(quests), "q617-q627")

    def test_freshness_verdict_ladder(self):
        not_configured = {"configured": False, "max_age_hours": 24, "age_hours": None, "fresh": None}
        self.assertIn("not configured", _freshness_verdict(not_configured))
        fresh = {"configured": True, "max_age_hours": 24, "age_hours": 0.36, "fresh": True}
        self.assertIn("OK", _freshness_verdict(fresh))
        stale_age = {"configured": True, "max_age_hours": 24, "age_hours": 26.0, "fresh": False}
        verdict = _freshness_verdict(stale_age)
        self.assertIn("STALE WARN", verdict)
        self.assertIn("NEVER auto-syncs", verdict)
        stale_flag = {"configured": True, "max_age_hours": 24, "age_hours": None, "fresh": False}
        self.assertIn("STALE WARN", _freshness_verdict(stale_flag))


class TestUnionMergePath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self._tmp.name)
        (self.wt / "templates").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _run_union(self, base, ours, theirs, merge_result=None, path="templates/a.html", add_calls=None):
        """Run _union_merge_path against scripted git blobs."""

        def responder(cmd, cwd=None, **kw):
            if cmd[:2] == ["git", "show"]:
                blob = {"base": base, "ours": ours, "theirs": theirs}[BLOB[cmd[2].split(":")[1]]]
                return _ok(blob)
            if cmd[:3] == ["git", "merge-file", "--union"]:
                return _ok(merge_result) if merge_result is not None else _fail("no merge output")
            if cmd[:2] == ["git", "add"]:
                if add_calls is not None:
                    add_calls.append(cmd[-1])
                return _ok()
            return _ok()

        with patch("court.cli.git_ops._run", side_effect=responder):
            return _union_merge_path(self.wt, path)

    def test_identical_sides_staged_as_is(self):
        staged = []
        ok, note = self._run_union("same\n", "same\n", "same\n", add_calls=staged)
        self.assertTrue(ok)
        self.assertIn("identical", note)
        self.assertEqual((self.wt / "templates/a.html").read_text(), "same\n")
        self.assertEqual(staged, ["templates/a.html"])

    def test_add_add_conflict_isolated_not_unioned(self):
        def responder(cmd, cwd=None, **kw):
            if cmd[:2] == ["git", "show"]:
                if cmd[2].startswith(":1:"):
                    return _fail("no such stage")  # no base — add/add shape
                return _ok("A-side\n" if cmd[2].startswith(":2:") else "B-side\n")
            return _ok()

        with patch("court.cli.git_ops._run", side_effect=responder):
            ok, note = _union_merge_path(self.wt, "templates/new.html")
        self.assertFalse(ok)
        self.assertIn("add/add", note)

    def test_modify_delete_conflict_isolated(self):
        def responder(cmd, cwd=None, **kw):
            if cmd[:2] == ["git", "show"]:
                return _fail("path not in stage")  # ours missing
            return _ok()

        with patch("court.cli.git_ops._run", side_effect=responder):
            ok, note = _union_merge_path(self.wt, "templates/gone.html")
        self.assertFalse(ok)
        self.assertIn("modify/delete", note)

    def test_union_resolves_both_sides_and_stages(self):
        staged = []
        ok, note = self._run_union("a\nb\nc\n", "a\nZ1\nb\nc\n", "a\nZ2\nb\nc\n",
                                   merge_result="a\nZ1\nb\nc\nZ2\n", add_calls=staged)
        self.assertTrue(ok)
        self.assertIn("union", note)
        self.assertEqual((self.wt / "templates/a.html").read_text(), "a\nZ1\nb\nc\nZ2\n")
        self.assertEqual(staged, ["templates/a.html"])

    def test_union_with_markers_is_rejected(self):
        ok, note = self._run_union("a\n", "b\n", "c\n",
                                   merge_result="<<<<<<< ours\nb\n=======\nc\n>>>>>>> theirs\n")
        self.assertFalse(ok)
        self.assertIn("markers", note)

    def test_python_syntax_guard(self):
        ok, note = self._run_union("def f():\n    pass\n", "def f(:\n", "    pass\n",
                                   merge_result="def f(:\n    pass\n", path="apps/web/views.py")
        self.assertFalse(ok)
        self.assertIn("syntax", note)


class TestMergeQuestBranchesWithPolicy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_paperwork_branch_wins_and_union_disclosed(self):
        calls = []
        (self.wt / "templates").mkdir(parents=True)

        def responder(cmd, cwd=None, **kw):
            calls.append(list(cmd))
            if cmd[:2] == ["git", "merge"]:
                return _fail("CONFLICT") if "quest/q602-ui" in cmd[2] else _ok()
            if cmd[:3] == ["git", "diff", "--name-only"]:
                return _ok(".court/quests/Q602-Web-Ui.md\ntemplates/a.html")
            if cmd[:3] == ["git", "checkout", "--theirs"]:
                return _ok()
            if cmd[:2] == ["git", "show"]:
                blob = {"1": "a\nb\nc\n", "2": "a\nZ1\nb\nc\n", "3": "a\nZ2\nb\nc\n"}
                return _ok(blob[cmd[2].split(":")[1]])
            if cmd[:3] == ["git", "merge-file", "--union"]:
                return _ok("a\nZ1\nb\nc\nZ2\n")
            return _ok()  # add, commit --no-edit, etc.

        with patch("court.cli.git_ops._run", side_effect=responder):
            merged, reports = _merge_quest_branches_with_policy(
                [_make_ui_quest("Q601-Web-Ui"), _make_ui_quest("Q602-Web-Ui")], self.wt, "q601-q602")

        self.assertEqual([q.id for q in merged], ["Q601-Web-Ui", "Q602-Web-Ui"])
        r2 = next(r for r in reports if r["id"] == "Q602-Web-Ui")
        self.assertEqual(r2["paperwork"], [".court/quests/Q602-Web-Ui.md"])
        self.assertEqual(len(r2["union"]), 1)
        self.assertIn("templates/a.html", r2["union"][0])
        self.assertIsNone(r2["isolated"])
        self.assertIn(["git", "commit", "--no-edit"], calls)

    def test_unresolvable_conflict_isolates_branch(self):
        calls = []

        def responder(cmd, cwd=None, **kw):
            calls.append(list(cmd))
            if cmd[:2] == ["git", "merge"]:
                return _fail("CONFLICT")
            if cmd[:3] == ["git", "diff", "--name-only"]:
                return _ok("apps/core/tricky.py")
            if cmd[:2] == ["git", "show"]:
                return _fail("missing stage")  # modify/delete shape
            return _ok()  # merge --abort, reset --hard

        with patch("court.cli.git_ops._run", side_effect=responder):
            merged, reports = _merge_quest_branches_with_policy(
                [_make_ui_quest("Q603-Web-Ui")], self.wt, "q603")

        self.assertEqual(merged, [])
        self.assertIsNotNone(reports[0]["isolated"])
        self.assertIn("unresolved conflicts", reports[0]["isolated"])
        self.assertIn(["git", "merge", "--abort"], calls)
        self.assertIn(["git", "reset", "--hard", "HEAD"], calls)

    def test_hard_merge_failure_isolates_without_conflicts(self):
        def responder(cmd, cwd=None, **kw):
            if cmd[:2] == ["git", "merge"]:
                return _fail("fatal: not something we can merge")
            return _ok()

        with patch("court.cli.git_ops._run", side_effect=responder):
            merged, reports = _merge_quest_branches_with_policy(
                [_make_ui_quest("Q604-Web-Ui")], self.wt, "q604")

        self.assertEqual(merged, [])
        self.assertIsNotNone(reports[0]["isolated"])
        self.assertNotIn("unresolved conflicts", reports[0]["isolated"])


class TestStudioCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.court_dir = self.tmp_path / ".court"
        (self.court_dir / "quests").mkdir(parents=True)
        (self.tmp_path / ".env").write_text("QSTASH_URL=https://example\n", encoding="utf-8")
        self.git = _GitRecorder()

    def tearDown(self):
        self._tmp.cleanup()

    def _patches(self, port=8250):
        return [
            patch("court.store.get_court_root", return_value=self.court_dir),
            patch("court.cli.git_ops.get_repo_root", return_value=self.tmp_path),
            patch("court.cli.git_ops._run", side_effect=self.git),
            patch("court.ward.audit_quest", return_value=MagicMock(violations=[], git_status={"dirty": False})),
            patch("court.cli._verify_branch_exists", side_effect=_verify_quest_branches_only),
            patch("court.cli.find_kilo_binary", return_value=None),
            patch("court.cli.migration_guard.scan_branch_contraband", return_value=[]),
            patch("court.cli._ensure_worktree_server", return_value=(port, f"http://localhost:{port}", "test server")),
            patch("court.cli._run_freshness_gate", return_value={
                "configured": True, "command": "python3 scripts/db/local_db.py --age",
                "max_age_hours": 24, "age_hours": 0.36, "fresh": True, "detail": "Freshness: FRESH (<24h)",
            }),
        ]

    def _save(self, *quests):
        for q in quests:
            store.save(q, court_root=self.court_dir, auto_commit=False)

    def _start_stop(self, ctxs):
        for c in ctxs:
            c.start()
        return ctxs

    def test_gates_skip_unaudited_and_approved_ui(self):
        q1 = _make_ui_quest("Q701-Web-Ui", pending=True)
        q2 = _make_ui_quest("Q702-Web-Ui", pending=False)
        q3 = _make_ui_quest("Q703-Web-Ui", pending=True)
        q3.body_sections[AUDIT_KEY] = ""
        self._save(q1, q2, q3)

        with patch("court.cli._merge_quest_branches_with_policy", return_value=([q1], [
            {"id": q1.id, "paperwork": [], "union": [], "isolated": None}
        ])) as mock_merge:
            ctxs = self._start_stop(self._patches())
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = main(["studio", "Q701-Web-Ui,Q702-Web-Ui,Q703-Web-Ui", "--port", "8250", "--no-commit"])
            finally:
                for c in ctxs:
                    c.stop()

        self.assertIn(rc, (0, None))
        out = buf.getvalue()
        self.assertIn("Q702-Web-Ui", out)  # approved UI -> skipped with reason
        self.assertIn("Q703-Web-Ui", out)  # unaudited -> refused
        merged_ids = [q.id for q in mock_merge.call_args[0][0]]
        self.assertEqual(merged_ids, ["Q701-Web-Ui"])

    def test_happy_path_worktree_merge_env_server_ledger(self):
        q1 = _make_ui_quest("Q711-Web-Ui")
        q2 = _make_ui_quest("Q712-Web-Ui")
        self._save(q1, q2)
        reports = [
            {"id": q1.id, "paperwork": [".court/quests/Q711-Web-Ui.md"], "union": [], "isolated": None},
            {"id": q2.id, "paperwork": [], "union": ["templates/a.html — union of both sides"], "isolated": None},
        ]

        with patch("court.cli._merge_quest_branches_with_policy", return_value=([q1, q2], reports)):
            ctxs = self._start_stop(self._patches())
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = main(["studio", "Q711-Web-Ui,Q712-Web-Ui", "--port", "8250", "--no-commit"])
            finally:
                for c in ctxs:
                    c.stop()

        self.assertIn(rc, (0, None))
        out = buf.getvalue()
        self.assertIn("artist-studio-q711-q712", out)
        self.assertIn("artist/q711-q712-ui-studio", out)

        # worktree created from castle tip via git worktree add + ff-only
        wt_add = [c for c in self.git.calls if c[:3] == ["git", "worktree", "add"]]
        self.assertTrue(wt_add, self.git.calls[:6])
        self.assertIn("-b", wt_add[0])
        self.assertIn("artist/q711-q712-ui-studio", wt_add[0])
        self.assertIn("castle", wt_add[0])
        self.assertIn(["git", "merge", "castle", "--ff-only"], self.git.calls)

        # no cogship stamp, no status change — the studio sits beside the pipeline
        lq1 = store.load("Q711-Web-Ui", court_root=self.court_dir)
        lq2 = store.load("Q712-Web-Ui", court_root=self.court_dir)
        self.assertEqual(lq1.status, "TRIBUTE_READY")
        self.assertEqual(lq2.status, "TRIBUTE_READY")
        self.assertEqual(lq1.cogship_id, "")
        self.assertEqual(lq1.artist_model, "openrouter/z-ai/glm-5.3")
        self.assertIn("combined artist studio q711-q712", lq1.body_sections.get("Castle Ledger", ""))
        self.assertIn("branch-wins", lq1.body_sections.get("Castle Ledger", ""))
        self.assertIn("union-resolved", lq2.body_sections.get("Castle Ledger", ""))

        # env plumbing + agent config + brief
        wt = self.tmp_path / ".kilo" / "worktrees" / "artist-studio-q711-q712"
        self.assertTrue((wt / ".env").exists())
        self.assertTrue((wt / ".kilo" / "kilo.json").is_file())
        kilo_cfg = json.loads((wt / ".kilo" / "kilo.json").read_text())
        self.assertEqual(kilo_cfg.get("default_agent"), "artist")
        brief = (wt / ".kilo" / "TASK_ARTIST.md").read_text()
        self.assertIn("q711-q712", brief)
        self.assertIn("Single-Writer Rule", brief)
        self.assertIn("Addendum-Quests:", brief)
        self.assertIn(".court/quests/Q711-Web-Ui.md", brief)  # branch-wins disclosure
        self.assertIn("templates/a.html", brief)  # union disclosure
        self.assertIn("sync-back", brief)
        self.assertIn("http://localhost:8250", brief)
        self.assertIn("Data freshness", brief)

    def test_json_payload_for_agent_manager_spawn(self):
        q1 = _make_ui_quest("Q721-Web-Ui")
        self._save(q1)
        reports = [{"id": q1.id, "paperwork": [], "union": [], "isolated": None}]

        with patch("court.cli._merge_quest_branches_with_policy", return_value=([q1], reports)):
            ctxs = self._start_stop(self._patches())
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = main(["studio", "Q721-Web-Ui", "--port", "8250", "--json", "--no-commit"])
            finally:
                for c in ctxs:
                    c.stop()

        self.assertIn(rc, (0, None))
        data = json.loads(buf.getvalue())
        self.assertEqual(data["studio"], "q721")
        self.assertEqual(data["branch"], "artist/q721-ui-studio")
        self.assertEqual(data["task"]["branchName"], "artist/q721-ui-studio")
        self.assertEqual(data["model"], "openrouter/z-ai/glm-5.3")
        self.assertIn("prompt", data["task"])
        self.assertEqual(data["freshness"]["age_hours"], 0.36)
        self.assertIn("conflict_policy", data)

    def test_standup_records_session_on_all_merged_quests(self):
        q1 = _make_ui_quest("Q731-Web-Ui")
        q2 = _make_ui_quest("Q732-Web-Ui")
        self._save(q1, q2)
        reports = [
            {"id": q1.id, "paperwork": [], "union": [], "isolated": None},
            {"id": q2.id, "paperwork": [], "union": [], "isolated": None},
        ]

        with patch("court.cli._merge_quest_branches_with_policy", return_value=([q1, q2], reports)), \
             patch("court.cli.standup_kilo_session", return_value={
                 "ok": True, "mode": "cli", "session_id": "ses_studio_123", "message": "spawned"
             }) as mock_spawn:
            ctxs = self._start_stop(self._patches())
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = main(["studio", "Q731-Web-Ui,Q732-Web-Ui", "--port", "8250", "--standup", "--no-commit"])
            finally:
                for c in ctxs:
                    c.stop()

        self.assertIn(rc, (0, None))
        self.assertTrue(mock_spawn.called)
        self.assertEqual(mock_spawn.call_args[1]["agent"], "artist")
        self.assertEqual(mock_spawn.call_args[1]["provider_hint"], "artist")
        lq1 = store.load("Q731-Web-Ui", court_root=self.court_dir)
        lq2 = store.load("Q732-Web-Ui", court_root=self.court_dir)
        self.assertEqual(lq1.artist_session_id, "ses_studio_123")
        self.assertEqual(lq2.artist_session_id, "ses_studio_123")

    def test_existing_worktree_guard(self):
        q1 = _make_ui_quest("Q741-Web-Ui")
        self._save(q1)
        wt = self.tmp_path / ".kilo" / "worktrees" / "artist-studio-q741"
        wt.mkdir(parents=True)

        from contextlib import redirect_stderr
        ctxs = self._start_stop(self._patches())
        try:
            buf_err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(buf_err), self.assertRaises(SystemExit):
                main(["studio", "Q741-Web-Ui", "--no-server", "--no-commit"])
        finally:
            for c in ctxs:
                c.stop()
        self.assertIn("already exists", buf_err.getvalue())

    def test_branch_namespace_guard(self):
        q1 = _make_ui_quest("Q742-Web-Ui")
        self._save(q1)

        from contextlib import redirect_stderr
        ctxs = self._start_stop(self._patches())
        try:
            buf_err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(buf_err), self.assertRaises(SystemExit):
                main(["studio", "Q742-Web-Ui", "--branch", "quest/q742-studio", "--no-server", "--no-commit"])
        finally:
            for c in ctxs:
                c.stop()
        self.assertIn("artist/", buf_err.getvalue())


class TestStudioSyncBack(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.court_dir = self.tmp_path / ".court"
        (self.court_dir / "quests").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, responder, quest_id="Q751-Web-Ui", branch="artist/q751-ui-studio"):
        calls = []

        def recording(cmd, cwd=None, **kw):
            calls.append(list(cmd))
            return responder(cmd)

        with patch("court.store.get_court_root", return_value=self.court_dir), \
             patch("court.cli.git_ops.get_repo_root", return_value=self.tmp_path), \
             patch("court.cli.git_ops._run", side_effect=recording), \
             patch("court.cli._verify_branch_exists", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(["studio", quest_id, "--sync-back", "--branch", branch, "--no-commit"])
        return rc, buf.getvalue(), calls

    def test_missing_studio_branch_exits(self):
        q = _make_ui_quest("Q751-Web-Ui", branch="quest/q751-ui", worktree="")
        store.save(q, court_root=self.court_dir, auto_commit=False)

        from contextlib import redirect_stderr
        with patch("court.store.get_court_root", return_value=self.court_dir), \
             patch("court.cli.git_ops.get_repo_root", return_value=self.tmp_path), \
             patch("court.cli._verify_branch_exists", return_value=False):
            buf_err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(buf_err), self.assertRaises(SystemExit):
                main(["studio", "Q751-Web-Ui", "--sync-back", "--branch", "artist/q751-ui-studio",
                      "--no-commit"])
        self.assertIn("not found", buf_err.getvalue())

    def test_clean_worktree_merges_and_logs(self):
        qwt = self.tmp_path / "wt-q751"
        qwt.mkdir()
        q = _make_ui_quest("Q751-Web-Ui", branch="quest/q751-ui", worktree=str(qwt))
        store.save(q, court_root=self.court_dir, auto_commit=False)

        def responder(cmd, cwd=None, **kw):
            if cmd[:2] == ["git", "status"]:
                return _ok("")  # clean
            if cmd[:2] == ["git", "merge"]:
                return _ok("Fast-forward")
            return _ok()

        rc, out, calls = self._run(responder)
        self.assertIn(rc, (0, None))
        self.assertIn("MERGED", out)
        merges = [c for c in calls if c[:2] == ["git", "merge"]]
        self.assertTrue(merges and merges[0][2] == "artist/q751-ui-studio")
        lq = store.load("Q751-Web-Ui", court_root=self.court_dir)
        self.assertIn("Synced combined studio artist/q751-ui-studio", lq.body_sections.get("Castle Ledger", ""))

    def test_dirty_worktree_skipped(self):
        qwt = self.tmp_path / "wt-q752"
        qwt.mkdir()
        q = _make_ui_quest("Q752-Web-Ui", branch="quest/q752-ui", worktree=str(qwt))
        store.save(q, court_root=self.court_dir, auto_commit=False)

        def responder(cmd, cwd=None, **kw):
            if cmd[:2] == ["git", "status"]:
                return _ok(" M templates/x.html\n")
            return _ok()

        rc, out, calls = self._run(responder, quest_id="Q752-Web-Ui", branch="artist/q752-ui-studio")
        self.assertIn(rc, (0, None))
        self.assertIn("SKIPPED", out)
        self.assertFalse([c for c in calls if c[:2] == ["git", "merge"]])

    def test_conflict_aborts_and_reports_without_forcing(self):
        qwt = self.tmp_path / "wt-q753"
        qwt.mkdir()
        q = _make_ui_quest("Q753-Web-Ui", branch="quest/q753-ui", worktree=str(qwt))
        store.save(q, court_root=self.court_dir, auto_commit=False)
        calls = []

        def responder(cmd, cwd=None, **kw):
            calls.append(list(cmd))
            if cmd[:2] == ["git", "status"]:
                return _ok("")
            if cmd[:2] == ["git", "merge"]:
                return _fail("CONFLICT")
            if cmd[:3] == ["git", "diff", "--name-only"]:
                return _ok("templates/a.html")
            return _ok()  # abort + reset

        rc, out, _ = self._run(responder, quest_id="Q753-Web-Ui", branch="artist/q753-ui-studio")
        self.assertIn(rc, (0, None))
        self.assertIn("CONFLICT", out)
        self.assertIn("templates/a.html", out)
        self.assertIn(["git", "merge", "--abort"], calls)
        self.assertIn(["git", "reset", "--hard", "HEAD"], calls)
        self.assertNotIn("MERGED", out)


class TestStudioDocsAndSpawnPaths(unittest.TestCase):
    def test_command_docs_present_in_both_layouts_and_document_both_spawn_paths(self):
        for p in (Path("court/commands/studio.md"), Path(".kilo/commands/studio.md")):
            self.assertTrue(p.exists(), p)
            text = p.read_text()
            self.assertIn("agent_manager", text)  # Branch A (AM-first)
            self.assertIn("kilo run --agent artist", text)  # Branch B (CLI fallback)
            self.assertIn("--sync-back", text)
            self.assertIn("branch-wins", text)
            self.assertIn("union", text)
            self.assertIn("freshness", text.lower())
            self.assertIn("studio.freshness_command", text)

    def test_studio_template_carries_review_contract(self):
        text = Path("court/templates/court_artist_studio_prompt.md").read_text()
        self.assertIn("Single-Writer Rule", text)
        self.assertIn("Addendum-Quests:", text)
        self.assertIn("union-resolved", text)
        self.assertIn("Zero Roleplay Jargon Leakage", text)
        self.assertIn("sync-back", text)
        self.assertIn("{{ merge_disclosures }}", text)
        self.assertIn("{{ freshness_note }}", text)


if __name__ == "__main__":
    unittest.main()
