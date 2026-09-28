"""Tests for the studio close-out lifecycle (`court studio <ids> --close`).

Built on REAL temp git repositories (no git mocking): the guards are git
mechanics (merge-base drift, patch-id equivalence, ancestor containment,
cherry-pick conflicts), so the fixtures exercise the actual commands.
"""
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from court import cli, store
from court.models import Quest
from court.studio_close import (
    all_green,
    apply_signoff,
    approved_ui_present,
    base_drift_count,
    cherry_pick_labeled,
    commit_names_quest,
    drift_threshold,
    gatehouse_race,
    labeled_commits,
    render_manifest,
    signoff_line,
)

SLUG = "q617-q627"
STUDIO_BRANCH = f"artist/{SLUG}-ui-studio"
QID = "Q617-X"
QID2 = "Q627-Y"


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    res = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {res.stderr}")
    return res.stdout


class StudioCloseFixture(unittest.TestCase):
    """A real repo shaped like a one-extraction studio cohort: castle <- two
    quest branches; studio branch cut from castle with both merged and one
    labeled artist commit on top."""

    def setUp(self) -> None:
        self._old_cwd = os.getcwd()
        self.root = Path(tempfile.mkdtemp(prefix="studio_close_test_"))
        _git(self.root, "init", "-q", "-b", "castle")
        _git(self.root, "config", "user.email", "test@example.com")
        _git(self.root, "config", "user.name", "Test")
        # Court/AM fixtures live inside the repo; never let git commit them
        # (a branch checkout would otherwise delete them from the tree).
        (self.root / ".gitignore").write_text(".court/\n.kilo/\nwt-*/\n")
        (self.root / "base.txt").write_text("base\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "castle base")

        # Quest branch with its UI feature
        _git(self.root, "checkout", "-qb", "quest/q617-x")
        (self.root / "ui").mkdir()
        (self.root / "ui" / "widget.html").write_text("widget v1\nline2 base\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "q617: widget feature")

        # Second quest branch (cohort shape), untouched by the artist
        _git(self.root, "checkout", "-qb", "quest/q627-y", "castle")
        (self.root / "other.html").write_text("q627 feature\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "q627: other feature")

        # Studio branch cut from castle, both quests merged, artist labeled commit
        _git(self.root, "checkout", "-qb", STUDIO_BRANCH, "castle")
        _git(self.root, "merge", "quest/q617-x", "--no-edit", "-q")
        _git(self.root, "merge", "quest/q627-y", "--no-edit", "-q")
        (self.root / "ui" / "widget.html").write_text("widget v1 + approved badge\nline2 base\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "style(x): Q617 royal review rev 1 — approved badge order")

        # Quest worktrees (extraction targets)
        self.wt = self.root / "wt-q617"
        _git(self.root, "worktree", "add", "-q", str(self.wt), "quest/q617-x")

        # Court + Agent Manager fixtures
        self.court = self.root / ".court"
        (self.court / "quests").mkdir(parents=True)
        studio_wt = self.root / ".kilo" / "worktrees" / f"artist-studio-{SLUG}"
        studio_wt.mkdir(parents=True)
        (self.root / ".kilo" / "agent-manager.json").write_text(json.dumps({
            "sections": {"sec_ash": {"name": "Ashes"}},
            "worktrees": {"wt_studio": {"path": str(studio_wt), "branch": STUDIO_BRANCH}},
            "sessions": {"ses_artist": {"worktreeId": "wt_studio"}},
        }))

        self._old_env = os.environ.get("COURT_DIR")
        os.environ["COURT_DIR"] = str(self.court)

        for qid, branch in ((QID, "quest/q617-x"), (QID2, "quest/q627-y")):
            q = Quest(
                id=qid, title=f"UI {qid}", kind="quest", app="web", concern="ui",
                status="GATE", branch=branch, section="Feature",
                worktree=str(self.wt) if qid == QID else str(self.root / "wt-q627"),
                artist_session_id="ses_artist",
                body_sections={"Castle Ledger": ""},
            )
            store.save(q, auto_commit=False)

        os.chdir(self.root)

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        if self._old_env is None:
            os.environ.pop("COURT_DIR", None)
        else:
            os.environ["COURT_DIR"] = self._old_env
        subprocess.run(["rm", "-rf", str(self.root)], check=False)

    @contextmanager
    def _captured(self):
        buf = StringIO()
        with patch("sys.stdout", buf):
            yield buf

    def _close(self, *extra: str):
        out_buf, err = StringIO(), StringIO()
        with self._captured() as out:
            with patch("sys.stderr", err):
                try:
                    cli.main(["studio", f"{QID},{QID2}", "--close", "--no-commit", *extra])
                    code = 0
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def _quest_side_conflict(self) -> None:
        """Move the quest branch past the reviewed state, editing the same
        line the artist's labeled commit changed -> real cherry-pick conflict."""
        (self.wt / "ui" / "widget.html").write_text("quest-side feature overwrites badge line\nline2 base\n")
        _git(self.wt, "add", "-A")
        _git(self.wt, "commit", "-qm", "q617: shipped feature after studio cut")


# ---------------------------------------------------------------------------
# Pure label / guard mechanics
# ---------------------------------------------------------------------------

class TestLabelMatching(unittest.TestCase):
    def test_subject_match(self):
        self.assertTrue(commit_names_quest("style(x): Q617 royal review rev 3", "", "Q617-Shops-Badge-Order"))
        self.assertTrue(commit_names_quest("STYLE(css): q617 ROYAL REVIEW rev 1", "", "Q617-X"))

    def test_subject_requires_qid_and_review_phrase(self):
        self.assertFalse(commit_names_quest("style(x): royal review rev 1", "", "Q617-X"))
        self.assertFalse(commit_names_quest("style(x): Q617 tweak", "", "Q617-X"))

    def test_addendum_trailer_match(self):
        body = "some polish\n\nAddendum-Quests: Q627, Q617\n"
        self.assertTrue(commit_names_quest("polish title", body, "Q617-X"))
        self.assertFalse(commit_names_quest("polish title", body, "Q699-Z"))


class TestSignoffGuard(unittest.TestCase):
    def test_missing_then_written(self):
        q = Quest(id="Q1-T", title="t", kind="quest", app="a", concern="c",
                  status="GATE", body_sections={"Castle Ledger": ""})
        self.assertIsNone(signoff_line(q))
        apply_signoff(q, "ship it")
        line = signoff_line(q)
        self.assertIsNotNone(line)
        self.assertIn("studio sign-off", line.lower())
        self.assertIn("ship it", line)
        self.assertRegex(line, r"\d{4}-\d{2}-\d{2}")


class TestDriftAndThreshold(StudioCloseFixture):
    def test_drift_counts_castle_ahead_commits(self):
        self.assertEqual(base_drift_count(self.root, STUDIO_BRANCH, "castle"), 0)
        _git(self.root, "checkout", "-q", "castle")
        (self.root / "base.txt").write_text("base v2\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "castle move 1")
        (self.root / "base.txt").write_text("base v3\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "castle move 2")
        _git(self.root, "checkout", "-q", STUDIO_BRANCH)
        self.assertEqual(base_drift_count(self.root, STUDIO_BRANCH, "castle"), 2)

    def test_threshold_precedence(self):
        self.assertEqual(drift_threshold(type("A", (), {"drift_threshold": None})()), 100)
        (self.court / "config.json").write_text(json.dumps({"studio": {"close_max_base_drift": 7}}))
        self.assertEqual(drift_threshold(type("A", (), {"drift_threshold": None})()), 7)
        self.assertEqual(drift_threshold(type("A", (), {"drift_threshold": 3})()), 3)


class TestConvoyRace(StudioCloseFixture):
    def test_no_race_without_gatehouse(self):
        q = store.load(QID)
        self.assertIsNone(gatehouse_race(self.root, q))

    def test_race_when_merged_onto_gatehouse_branch(self):
        _git(self.root, "checkout", "-qb", "the-gatehouse/cogship-243", "quest/q617-x")
        (self.root / "gh.txt").write_text("convoy\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "convoy pack")
        _git(self.root, "checkout", "-q", STUDIO_BRANCH)
        why = gatehouse_race(self.root, store.load(QID))
        self.assertIsNotNone(why)
        self.assertIn("the-gatehouse/cogship-243", why)

    def test_race_when_cogship_stamped_and_branch_exists(self):
        _git(self.root, "branch", "the-gatehouse/cogship-999", "castle")
        q = store.load(QID)
        q.cogship_id = "cogship-999"
        why = gatehouse_race(self.root, q)
        self.assertIsNotNone(why)
        self.assertIn("cogship-999", why)


# ---------------------------------------------------------------------------
# Extraction mechanics
# ---------------------------------------------------------------------------

class TestExtraction(StudioCloseFixture):
    def test_labeled_commits_found_oldest_first_with_files(self):
        out = labeled_commits(self.root, STUDIO_BRANCH, [QID, QID2], "castle")
        self.assertEqual(len(out[QID]), 1)
        c = out[QID][0]
        self.assertIn("royal review", c["subject"])
        self.assertIn("ui/widget.html", c["files"])
        self.assertEqual(out[QID2], [])

    def test_clean_cherry_pick_records_provenance_and_content(self):
        commits = labeled_commits(self.root, STUDIO_BRANCH, [QID], "castle")[QID]
        report = cherry_pick_labeled(self.root, str(self.wt), commits)
        self.assertEqual(report["status"], "picked")
        self.assertEqual(len(report["picked"]), 1)
        msg = _git(self.root, "log", "-1", "--format=%B", "quest/q617-x")
        self.assertIn("cherry picked from commit", msg)
        self.assertIn("approved badge", (self.wt / "ui" / "widget.html").read_text())
        self.assertIn("line2 base", (self.wt / "ui" / "widget.html").read_text())

    def test_conflict_aborts_clean_and_never_unions(self):
        self._quest_side_conflict()
        commits = labeled_commits(self.root, STUDIO_BRANCH, [QID], "castle")[QID]
        report = cherry_pick_labeled(self.root, str(self.wt), commits)
        self.assertEqual(report["status"], "conflict")
        self.assertIn("ui/widget.html", report["conflicts"])
        self.assertEqual(_git(self.wt, "status", "--porcelain").strip(), "")
        self.assertEqual(
            (self.wt / "ui" / "widget.html").read_text(),
            "quest-side feature overwrites badge line\nline2 base\n",
        )

    def test_approved_ui_present_patch_id_flow(self):
        commits = labeled_commits(self.root, STUDIO_BRANCH, [QID], "castle")[QID]
        ok, _ = approved_ui_present(self.root, "quest/q617-x", STUDIO_BRANCH, commits)
        self.assertFalse(ok)
        cherry_pick_labeled(self.root, str(self.wt), commits)
        ok, detail = approved_ui_present(self.root, "quest/q617-x", STUDIO_BRANCH, commits)
        self.assertTrue(ok)
        self.assertIn("patch-id-applied", detail)


# ---------------------------------------------------------------------------
# End-to-end close runs
# ---------------------------------------------------------------------------

class TestCloseEndToEnd(StudioCloseFixture):
    def test_guard1_refusal_without_signoff(self):
        code, out, err = self._close()
        self.assertEqual(code, 1)
        manifest = (self.root / ".court" / "studio-close" / SLUG / "manifest.md").read_text()
        self.assertIn("Verdict: BLOCKED", manifest)
        self.assertIn("sign-off", manifest)
        self.assertIn("refused", out)

    def test_happy_path_signoff_sync_manifest_teardown(self):
        code, out, err = self._close("--signoff", "royal ship it")
        self.assertEqual(code, 0, out)
        self.assertIn("ALL-GREEN", out)
        q = store.load(QID)
        self.assertIn("royal ship it", signoff_line(q) or "")
        self.assertIn("approved badge", (self.wt / "ui" / "widget.html").read_text())
        manifest = (self.root / ".court" / "studio-close" / SLUG / "manifest.md").read_text()
        self.assertIn("Verdict: ALL-GREEN", manifest)
        self.assertIn("| Q617-X | synced |", manifest)
        self.assertIn("| Q627-Y | already-present |", manifest)
        self.assertIn("Approved UI on branch", manifest)
        self.assertIn("agent_manager move", out)
        self.assertIn("sectionID=sec_ash", out)
        self.assertLess(out.index("agent_manager move"), out.index("agent_manager stop"))
        branch_alive = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", f"refs/heads/{STUDIO_BRANCH}"],
            cwd=str(self.root), capture_output=True,
        ).returncode == 0
        self.assertTrue(branch_alive, "studio branch ref must be kept")

    def test_json_output(self):
        code, out, err = self._close("--signoff", "ok", "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["verdict"], "ALL-GREEN")
        rows = {r["id"]: r for r in payload["rows"]}
        self.assertEqual(rows[QID]["outcome"], "synced")
        self.assertEqual(rows[QID]["ui_present"], "YES")
        self.assertEqual(rows[QID2]["outcome"], "already-present")
        self.assertIn("manifest", payload["manifest"])

    def test_drift_refusal_and_threshold_override(self):
        _git(self.root, "checkout", "-q", "castle")
        (self.root / "base.txt").write_text("base v9\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "castle far ahead")
        (self.root / "base.txt").write_text("base v10\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "castle farther ahead")
        _git(self.root, "checkout", "-q", STUDIO_BRANCH)
        (self.court / "config.json").write_text(json.dumps({"studio": {"close_max_base_drift": 1}}))
        code, out, err = self._close("--signoff", "ok")
        self.assertEqual(code, 1)
        self.assertIn("base drift 2 commits exceeds threshold 1", out)
        code, out, err = self._close("--signoff", "ok", "--drift-threshold", "50")
        self.assertEqual(code, 0, out)

    def test_convoy_race_blocks_quest(self):
        _git(self.root, "checkout", "-qb", "the-gatehouse/cogship-243", "quest/q617-x")
        (self.root / "gh.txt").write_text("convoy\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "convoy pack")
        _git(self.root, "checkout", "-q", STUDIO_BRANCH)
        code, out, err = self._close("--signoff", "ok")
        self.assertEqual(code, 1)
        self.assertIn("race-blocked", out)
        self.assertIn("the-gatehouse/cogship-243", out)

    def test_union_conflict_routes_to_brief_and_never_teardown(self):
        self._quest_side_conflict()
        code, out, err = self._close("--signoff", "ok")
        self.assertEqual(code, 0)  # close completes; union-pending is a routed outcome
        self.assertIn("union-pending", out)
        brief = self.root / ".court" / "studio-close" / SLUG / f"{QID}-union-brief.md"
        self.assertTrue(brief.exists())
        text = brief.read_text()
        for needle in ("Conflict regions", "Branch-side features the approved design predates",
                       "Approved-design changes on the studio branch", "quest-side feature"):
            self.assertIn(needle, text)
        q = store.load(QID)
        self.assertIn("Studio Close: UNION-PENDING", q.body_sections.get("Castle Ledger", ""))
        self.assertNotIn("ALL-GREEN", out)
        self.assertIn("locked", out)  # teardown gated off


class TestSyncBackGuards(StudioCloseFixture):
    def test_sync_back_refuses_on_drift(self):
        _git(self.root, "checkout", "-q", "castle")
        (self.root / "base.txt").write_text("moved\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "castle ahead")
        _git(self.root, "checkout", "-q", STUDIO_BRANCH)
        (self.court / "config.json").write_text(json.dumps({"studio": {"close_max_base_drift": 0}}))
        err = StringIO()
        with patch("sys.stdout", StringIO()), patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["studio", QID, "--sync-back", "--branch", STUDIO_BRANCH, "--no-commit"])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("base drift", err.getvalue())

    def test_sync_back_refuses_on_race(self):
        _git(self.root, "checkout", "-qb", "the-gatehouse/cogship-244", "quest/q617-x")
        (self.root / "gh2.txt").write_text("x\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "pack")
        _git(self.root, "checkout", "-q", STUDIO_BRANCH)
        err = StringIO()
        with patch("sys.stdout", StringIO()), patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["studio", QID, "--sync-back", "--branch", STUDIO_BRANCH, "--no-commit"])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("convoy-race", err.getvalue())


class TestManifestRendering(unittest.TestCase):
    def test_all_green_requires_ui_yes(self):
        rows = [{"id": "Q1", "outcome": "synced", "provenance": "h", "ui_present": "YES"}]
        self.assertTrue(all_green(rows))
        rows[0]["ui_present"] = "NO"
        self.assertFalse(all_green(rows))
        self.assertIn("Teardown locked", render_manifest(
            "s", "artist/x", Path("/wt"), "castle", rows, {"verdict": "BLOCKED"}))


if __name__ == "__main__":
    unittest.main()
