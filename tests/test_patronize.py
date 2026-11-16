"""Tests for the standing atelier patronage model (`court patronize`).

Built on REAL temp git repositories (no git mocking): patronage pins and
mechanical extraction are git mechanics (labeled-commit selection, cherry-pick,
patch-id equivalence), so the fixtures exercise the actual commands.

The central invariant under test: a patronage pin is a set of the Quest's OWN
artist-review commit SHAs, so extracting it never drags in another Quest's
in-progress work from the shared atelier tip.
"""
import os
import subprocess
import tempfile
import unittest
from argparse import Namespace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from court import atelier, cli, store
from court.models import Quest

Q1 = "Q1-Widget"
Q2 = "Q2-Other"
Q3 = "Q3-Unreviewed"
ATELIER = "artist/atelier"


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    res = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {res.stderr}")
    return res.stdout


def _show(cwd: Path, ref: str, path: str) -> str:
    return _git(cwd, "show", f"{ref}:{path}", check=False)


class AtelierFixture(unittest.TestCase):
    """castle <- two quest branches; atelier branch cut from castle with both
    merged and one labeled artist commit per quest."""

    def setUp(self) -> None:
        self._old_cwd = os.getcwd()
        self._old_env = os.environ.get("COURT_DIR")
        self.root = Path(tempfile.mkdtemp(prefix="patronize_test_"))
        _git(self.root, "init", "-q", "-b", "castle")
        _git(self.root, "config", "user.email", "test@example.com")
        _git(self.root, "config", "user.name", "Test")
        (self.root / ".gitignore").write_text(".court/\n.kilo/\nwt-*/\n")
        (self.root / "base.txt").write_text("base\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "castle base")

        _git(self.root, "checkout", "-qb", "quest/q1")
        (self.root / "ui").mkdir()
        (self.root / "ui" / "widget.html").write_text("widget v1\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "q1 widget")

        _git(self.root, "checkout", "-qb", "quest/q2", "castle")
        (self.root / "other.html").write_text("q2 feature\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "q2 feature")

        # Q3: a branch that was never merged into the atelier (no artist review).
        _git(self.root, "checkout", "-qb", "quest/q3", "castle")
        (self.root / "unreviewed.html").write_text("q3 feature\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "q3 feature")

        _git(self.root, "checkout", "-qb", ATELIER, "castle")
        _git(self.root, "merge", "quest/q1", "--no-edit", "-q")
        _git(self.root, "merge", "quest/q2", "--no-edit", "-q")
        (self.root / "ui" / "widget.html").write_text("widget v1 + badge\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "style(x): Q1 royal review rev 1 — badge")
        (self.root / "other.html").write_text("q2 feature + polish\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "style(x): Q2 royal review rev 1 — polish")

        self.wt1 = self.root / "wt-q1"
        self.wt2 = self.root / "wt-q2"
        self.wt3 = self.root / "wt-q3"
        _git(self.root, "worktree", "add", "-q", str(self.wt1), "quest/q1")
        _git(self.root, "worktree", "add", "-q", str(self.wt2), "quest/q2")
        _git(self.root, "worktree", "add", "-q", str(self.wt3), "quest/q3")

        self.court = self.root / ".court"
        (self.court / "quests").mkdir(parents=True)
        os.environ["COURT_DIR"] = str(self.court)
        for qid, branch, wt in ((Q1, "quest/q1", self.wt1), (Q2, "quest/q2", self.wt2),
                                (Q3, "quest/q3", self.wt3)):
            q = Quest(id=qid, title=f"UI {qid}", kind="quest", app="web", concern="ui",
                      status="GATE", branch=branch, section="Feature", worktree=str(wt),
                      body_sections={"Castle Ledger": ""})
            store.save(q, auto_commit=False)
        os.chdir(self.root)

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        if self._old_env is None:
            os.environ.pop("COURT_DIR", None)
        else:
            os.environ["COURT_DIR"] = self._old_env
        subprocess.run(["rm", "-rf", str(self.root)], check=False)

    def _patronize(self, qid: str, **overrides):
        args = Namespace(
            quest_id=qid, by="M'Lord", note="reviewed in the merged view",
            atelier=None, base="castle", revision="", allow_empty=False,
            extract=False, no_enqueue=True, json=False, no_commit=True,
        )
        for k, v in overrides.items():
            setattr(args, k, v)
        with patch("sys.stdout", StringIO()):
            cli.cmd_patronize(args)


class TestPatronagePin(AtelierFixture):
    def test_pin_records_only_this_quests_labeled_commits(self):
        commits = atelier.labeled_shas(self.root, ATELIER, Q1)
        self.assertEqual(len(commits), 1, "Q1 should have exactly one labeled artist commit")
        self.assertIn("Q1", commits[0]["subject"])

        pin = atelier.pin_quest(self.root, store.load(Q1), commits, by="M'Lord")
        self.assertEqual(len(pin["shas"]), 1)
        self.assertFalse(pin["extracted"])
        self.assertEqual(pin["by"], "M'Lord")
        # The pin is a SHA set, never the atelier tip.
        self.assertNotIn("atelier_tip", pin)

    def test_pin_is_self_contained(self):
        pin = atelier.pin_quest(
            self.root, store.load(Q1),
            atelier.labeled_shas(self.root, ATELIER, Q1), by="M'Lord",
        )
        rec = pin["commits"][0]
        self.assertTrue(rec["hash"])
        self.assertIn("ui/widget.html", rec["files"])

    def test_member_without_pin_is_detected(self):
        atelier.record_member(self.root, store.load(Q2), auto_commit=False)
        self.assertIsNotNone(atelier.member(self.root, Q2))
        self.assertIsNone(atelier.pin_for(self.root, Q2))


class TestMechanicalExtraction(AtelierFixture):
    def test_extraction_applies_only_the_pinned_commit(self):
        self._patronize(Q1)
        pin = atelier.pin_for(self.root, Q1)
        report = atelier.extract_quest(self.root, store.load(Q1), pin, auto_commit=False)
        self.assertEqual(report["status"], "extracted")

        # Q1's approved UI is now on its own branch ...
        self.assertIn("badge", _show(self.root, "quest/q1", "ui/widget.html"))
        # ... and Q2's in-progress artist change did NOT ride along: it lives
        # only on the shared atelier tip, never on Q1's branch.
        self.assertIn("polish", _show(self.root, ATELIER, "other.html"))
        self.assertNotIn("polish", _show(self.root, "quest/q1", "other.html"))

    def test_extraction_is_idempotent(self):
        self._patronize(Q1)
        pin = atelier.pin_for(self.root, Q1)
        first = atelier.extract_quest(self.root, store.load(Q1), pin, auto_commit=False)
        self.assertEqual(first["status"], "extracted")
        pin2 = atelier.pin_for(self.root, Q1)
        second = atelier.extract_quest(self.root, store.load(Q1), pin2, auto_commit=False)
        self.assertEqual(second["status"], "already-present")

    def test_empty_pin_is_a_noop_approval(self):
        pin = atelier.pin_quest(self.root, store.load(Q1), [], by="M'Lord",
                                atelier_branch=ATELIER)
        self.assertTrue(pin["empty"])
        self.assertTrue(pin["extracted"])
        report = atelier.extract_quest(self.root, store.load(Q1), pin, auto_commit=False)
        self.assertEqual(report["status"], "empty")


class TestPatronizeCommand(AtelierFixture):
    def test_patronize_writes_signoff_and_pin(self):
        self._patronize(Q1)
        q = store.load(Q1)
        audit = q.body_sections.get("Master of Coin's Audit", "")
        self.assertIn("UI Review:** APPROVED", audit)
        self.assertIn("UI Sign-off:**", audit)
        self.assertIsNotNone(atelier.pin_for(self.root, Q1))

    def test_patronize_queues_nightwatch_collect(self):
        self._patronize(Q1, no_enqueue=False)
        vigil = self.root / ".court" / "vigils" / "collect-q1.json"
        self.assertTrue(vigil.is_file(), "patronize should queue a collect vigil by default")

    def test_patronize_without_artist_commits_errors(self):
        # Q3 was never merged into the atelier, so it has no labeled commits.
        args = Namespace(
            quest_id=Q3, by="M'Lord", note="", atelier=None, base="castle",
            revision="", allow_empty=False, extract=False, no_enqueue=True,
            json=False, no_commit=True,
        )
        err = StringIO()
        with patch("sys.stdout", StringIO()), patch("sys.stderr", err):
            with self.assertRaises(SystemExit):
                cli.cmd_patronize(args)

    def test_explicit_revision_pin(self):
        commit = atelier.labeled_shas(self.root, ATELIER, Q1)[0]["hash"]
        self._patronize(Q1, revision=commit)
        pin = atelier.pin_for(self.root, Q1)
        self.assertEqual(pin["shas"], [commit])


class TestConflictPath(unittest.TestCase):
    """A genuine overlap on a shared file is a conflict (routed to the artist),
    never a silent import of the other Quest's work."""

    def setUp(self) -> None:
        self._old_cwd = os.getcwd()
        self._old_env = os.environ.get("COURT_DIR")
        self.root = Path(tempfile.mkdtemp(prefix="patronize_conflict_"))
        _git(self.root, "init", "-q", "-b", "castle")
        _git(self.root, "config", "user.email", "test@example.com")
        _git(self.root, "config", "user.name", "Test")
        (self.root / ".gitignore").write_text(".court/\n.kilo/\nwt-*/\n")
        (self.root / "shared.txt").write_text("line1\nline2\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "castle base")

        _git(self.root, "checkout", "-qb", "quest/q1")
        (self.root / "shared.txt").write_text("line1\nbranch-side\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "q1 branch edit")

        # Atelier: artist commit touching the same line.
        _git(self.root, "checkout", "-qb", ATELIER, "castle")
        (self.root / "shared.txt").write_text("line1\nartist-side\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "style(x): Q1 royal review rev 1 — shared line")

        self.wt1 = self.root / "wt-q1"
        _git(self.root, "worktree", "add", "-q", str(self.wt1), "quest/q1")

        self.court = self.root / ".court"
        (self.court / "quests").mkdir(parents=True)
        os.environ["COURT_DIR"] = str(self.court)
        store.save(Quest(id=Q1, title="UI", kind="quest", app="web", concern="ui",
                         status="GATE", branch="quest/q1", section="Feature",
                         worktree=str(self.wt1), body_sections={"Castle Ledger": ""}),
                   auto_commit=False)
        os.chdir(self.root)

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        if self._old_env is None:
            os.environ.pop("COURT_DIR", None)
        else:
            os.environ["COURT_DIR"] = self._old_env
        subprocess.run(["rm", "-rf", str(self.root)], check=False)

    def test_overlap_conflicts_and_worktree_is_restored(self):
        commits = atelier.labeled_shas(self.root, ATELIER, Q1)
        pin = atelier.pin_quest(self.root, store.load(Q1), commits, by="M'Lord",
                                atelier_branch=ATELIER)
        report = atelier.extract_quest(self.root, store.load(Q1), pin, auto_commit=False)
        self.assertEqual(report["status"], "conflict")
        # The branch-side edit is untouched — no blind union.
        self.assertEqual(_show(self.root, "quest/q1", "shared.txt"), "line1\nbranch-side\n")
        # Worktree restored clean (no cherry-pick in progress).
        self.assertEqual(_git(self.wt1, "status", "--porcelain").strip(), "")


class TestCollectWiring(AtelierFixture):
    """`court collect` is the deferred mechanical extraction step: an atelier
    member must be patronized, and a patronized member's pinned revision is
    extracted onto its own branch before packing."""

    def _collect_args(self, qid):
        return Namespace(
            quest_ids=qid, quest_id=None, base="castle", no_commit=True, regate=False,
            force=False, hotfix=False, skip_ui_review=False, allow_pending_studio=False,
            cogship=None, status_filter=None, app=None, epic=None, any_status=False,
            include_archived=False, standup=False,
        )

    def test_unpatronized_member_without_a_mandate_is_inventoried(self):
        """Membership alone is not a pack gate, and it is not "debt" either.

        With no patronage pin and no recorded mandate the Quest packs from its
        own branch and collect says so plainly. (Its UI review is clear here; a
        PENDING review would be refused above this block.)
        """
        atelier.record_member(self.root, store.load(Q1), auto_commit=False)
        out = StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", StringIO()):
            cli.cmd_collect(self._collect_args(Q1))
        text = out.getvalue()
        self.assertIn("no patronage pin", text)
        self.assertNotIn("not yet patronized", text)
        self.assertNotIn("not a pack gate", text)
        self.assertNotIn("tracked debt", text)

    def test_unpatronized_member_with_a_ui_mandate_is_refused(self):
        """A recorded UI MANDATE IS a hard pack gate: refuse until patronized."""
        atelier.record_member(self.root, store.load(Q1), auto_commit=False)
        qf = self.court / "quests" / "Q1-Widget.md"
        qf.write_text(qf.read_text(encoding="utf-8")
                      + "\n- **UI Mandate:** REQUIRED — testing mandate\n", encoding="utf-8")
        out = StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", StringIO()):
            cli.cmd_collect(self._collect_args(Q1))
        self.assertIn("not yet patronized", out.getvalue())

    def test_patronized_member_is_extracted_before_packing(self):
        self._patronize(Q1)
        self.assertNotIn("badge", _show(self.root, "quest/q1", "ui/widget.html"))
        out = StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", StringIO()):
            cli.cmd_collect(self._collect_args(Q1))
        # Extraction ran (the pin landed on Q1's branch) ...
        self.assertIn("extracted", out.getvalue())
        self.assertIn("badge", _show(self.root, "quest/q1", "ui/widget.html"))
        # ... and with no recorded MoC audit the Quest is then skipped, not packed.
        self.assertIn("Skipped 1 candidate", out.getvalue())


if __name__ == "__main__":
    unittest.main()
