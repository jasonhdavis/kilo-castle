"""Q1574 — UI-review-outstanding Quests route to the atelier, never a convoy.

The defect this pins down: the Night's Watch planner called a GATE Quest with
a PENDING UI review "not collectable", but ``court collect`` only printed a UI
DEBT warning and packed it anyway (Q1480/Q1559/Q887 rode into cogship-626 that
way), while ``court verify-convoy`` treated the same Quest as mere tracked debt.
The three paths now resolve ONE shared predicate in ``convoy_guard``, collect
refuses the candidate and names the standing atelier as its destination, and the
atelier merge lane is default-on so the routed Quest actually has somewhere to go.
"""
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from court import cli, convoy_guard
from court import nightwatch as nw
from court.models import Quest

def _engine_dir() -> Path:
    """Locate the engine package from either repository layout.

    pb-app vendors it at ``.court/engine``; the upstream engine repository keeps
    it at ``court``. Same tests, both trees.
    """
    root = Path(__file__).resolve().parents[1]
    vendored = root / "engine"
    return vendored if vendored.is_dir() else root / "court"


MODULES = (
    _engine_dir() / "convoy_guard.py",
    _engine_dir() / "cli.py",
    _engine_dir() / "nightwatch.py",
)


def _write_charter(repo_root, quest_id, ui_review_line, status="GATE"):
    """Write a minimal charter carrying one UI Review line into a temp repo."""
    directory = Path(repo_root) / ".court" / "quests"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{quest_id}-Test-Quest.md"
    path.write_text(
        "---\n"
        f"id: {quest_id}\n"
        "title: Test Quest\n"
        "kind: quest\n"
        "app: platform\n"
        "concern: Test\n"
        f"status: {status}\n"
        f"branch: quest/{quest_id.lower()}-test\n"
        "---\n\n"
        f"# {quest_id} — Test Quest\n\n"
        "## Master of Coin's Audit\n\n"
        "- **Verdict:** PASS\n"
        f"- **UI Review:** {ui_review_line}\n",
        encoding="utf-8",
    )
    return path


def _quest(quest_id="Q9901", status="TRIBUTE_READY"):
    return Quest(
        id=quest_id,
        title="Test Quest",
        app="platform",
        concern="Test",
        branch=f"quest/{quest_id.lower()}-test",
        status=status,
        body_sections={
            "The Kingdom Requires": "Scope",
            "Expected Tribute": "- [x] Done",
            "Tribute Rendered": (
                "## 1. Ballad\nDone\n## 2. Tribute\nDone\n## 3. Tally\nDone\n"
                "## 4. Penance\nNone\n## 5. Audience\nNone\n## 6. Humble Opinion\nDone\n"
            ),
            "Master of Coin's Audit": "- **Verdict:** PASS\n",
        },
    )


class SharedUiReviewPredicateTests(unittest.TestCase):
    """One classifier decides outstanding vs clear for every packing path."""

    def test_state_classification(self):
        self.assertEqual(convoy_guard.ui_review_state("PENDING (artist session)"), "pending")
        # A PENDING line that says defer is a deferral, matching ui_assent_state.
        self.assertEqual(convoy_guard.ui_review_state("PENDING — deferred while away"), "deferred")
        self.assertEqual(convoy_guard.ui_review_state("DEFERRED — close later"), "deferred")
        self.assertEqual(convoy_guard.ui_review_state("deferred to a later pass"), "deferred")
        self.assertEqual(convoy_guard.ui_review_state("APPROVED — signed by M'Lord"), "clear")
        self.assertEqual(convoy_guard.ui_review_state("None required"), "clear")
        self.assertEqual(convoy_guard.ui_review_state(""), "missing")

    def test_outstanding_covers_pending_and_deferred_only(self):
        self.assertTrue(convoy_guard.ui_review_outstanding("PENDING (APPROVED asserted without a sign-off)"))
        self.assertTrue(convoy_guard.ui_review_outstanding("DEFERRED — tracked debt"))
        self.assertFalse(convoy_guard.ui_review_outstanding("APPROVED — by M'Lord 2026-11-16"))
        self.assertFalse(convoy_guard.ui_review_outstanding("WAIVED — deliberate skip"))
        self.assertFalse(convoy_guard.ui_review_outstanding(""))

    def test_outstanding_for_reads_the_charter(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            _write_charter(tmp, "Q9901", "PENDING (no artist pass yet)")
            self.assertEqual(convoy_guard.ui_review_outstanding_for(tmp, "Q9901")[0], True)
            _write_charter(tmp, "Q9902", "DEFERRED — close after deploy")
            self.assertEqual(convoy_guard.ui_review_outstanding_for(tmp, "Q9902")[0], True)
            _write_charter(tmp, "Q9903", "APPROVED — signed by M'Lord 2026-11-16")
            self.assertEqual(convoy_guard.ui_review_outstanding_for(tmp, "Q9903")[0], False)

    def test_no_duplicated_string_matching_in_the_three_paths(self):
        """The predicate must live in convoy_guard, not be re-spelled per caller."""
        banned = ('startswith(("PENDING", "DEFERRED"))', "startswith(('PENDING', 'DEFERRED'))")
        for module in MODULES:
            text = module.read_text(encoding="utf-8")
            for needle in banned:
                self.assertNotIn(needle, text, f"{module.name} re-implements the UI predicate")
        cli_text = MODULES[1].read_text(encoding="utf-8")
        nw_text = MODULES[2].read_text(encoding="utf-8")
        self.assertIn("ui_review_outstanding_for", cli_text)
        self.assertIn("ui_review_outstanding", nw_text)


class CollectUiPendingGateTests(unittest.TestCase):
    """court collect must refuse a UI-pending candidate and name the atelier."""

    def _run_collect(self, tmp, allow=False, atelier_member=None):
        quest = _quest()
        args = MagicMock()
        args.base = "castle"
        args.no_commit = True
        args.force = False
        args.cogship = "cogship-999" if allow else None
        args.allow_ui_pending = allow
        args.skip_ui_review = False
        args.no_premerge = True
        args.hotfix = False
        args.regate = False
        args.allow_pending_studio = False
        args.collect_min_batch = 0
        args.max_convoys = 1
        args.standup = False

        buf = io.StringIO()
        with patch("court.cli.resolve_quest_selection", return_value=[quest]), \
             patch("court.git_ops.get_repo_root", return_value=Path(tmp)), \
             patch("court.cli.require_op_lock"), \
             patch.object(cli, "convoy_admission_error", return_value=None, create=True), \
             patch.object(convoy_guard, "convoy_admission", return_value={"in_flight": []}, create=True), \
             patch("court.cli.ward.audit_quest") as audit, \
             patch("court.cli.store.stamp_cogship", return_value="cogship-999") as stamp, \
             patch("court.cli.store.save_many"), \
             patch("court.cli.convoy_guard.record_pack", return_value=None), \
             patch("court.cli.atelier.member", return_value=atelier_member), \
             patch("court.cli.config.get_model", return_value="openrouter/test/model"), \
             redirect_stdout(buf):
            audit.return_value = MagicMock(git_status={"dirty": False}, violations=[])
            cli.cmd_collect(args)
        return buf.getvalue(), stamp

    def test_pending_and_deferred_candidates_pack_nothing(self):
        import tempfile

        for line in ("PENDING (artist session pending)", "DEFERRED — close later"):
            with self.subTest(ui_review=line):
                with tempfile.TemporaryDirectory() as tmp:
                    _write_charter(tmp, "Q9901", line)
                    output, stamp = self._run_collect(tmp, allow=False)
                stamp.assert_not_called()
                self.assertIn("not collectable", output)
                self.assertIn("atelier", output)
                self.assertIn("Q9901", output)
                # The old lie must be gone: debt wording no longer proceeds to pack.
                self.assertNotIn("does NOT block", output)

    def test_pending_atelier_member_never_reaches_the_debt_message(self):
        """The class the charter names (Q1480/Q1559/Q887): an ATELIER MEMBER.

        Pre-fix, ``cmd_collect`` ran the standing-atelier block first, so a
        UI-pending atelier member printed "in the standing atelier without a
        patronage pin, and the charter records no UI Mandate — treating the
        review as tracked debt (not a pack gate) and packing." and only THEN
        refused it in the same invocation. The gate must run above that block:
        the Quest is refused, the atelier is named, and neither debt message
        appears.
        """
        import tempfile

        # A member record with no patronage pin: exactly the shape that used to
        # reach the debt branch.
        member = {"quest_id": "Q9901", "atelier_branch": "artist/atelier", "patronage": None}
        with tempfile.TemporaryDirectory() as tmp:
            _write_charter(tmp, "Q9901", "PENDING (artist session pending)")
            output, stamp = self._run_collect(tmp, allow=False, atelier_member=member)

        stamp.assert_not_called()
        self.assertIn("not collectable", output)
        self.assertIn("atelier", output)
        self.assertIn("Q9901", output)
        # The contradictory line from the failed audit, and its sibling claim.
        self.assertNotIn("not a pack gate", output)
        self.assertNotIn("does NOT block", output)
        self.assertNotIn("tracked debt", output)

    def test_allow_ui_pending_override_packs(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            _write_charter(tmp, "Q9901", "PENDING (deliberate exception)")
            output, stamp = self._run_collect(tmp, allow=True)
        stamp.assert_called_once()
        self.assertIn("Stamped 1 Quest(s) onto cogship-999", output)
        self.assertIn("--allow-ui-pending", output)


class PlannerAndVerifyConvoyAgreeTests(unittest.TestCase):
    """Planner note and promotion guard resolve the same predicate."""

    def test_planner_routes_pending_to_the_atelier_lane(self):
        status = convoy_guard.ui_review_status_text(
            self._tmp_with("PENDING (artist session)"), "Q9901"
        )
        self.assertTrue(convoy_guard.ui_review_outstanding(status))

        snap = {
            "quests": [{
                "id": "Q9901", "kind": "quest", "status": "GATE",
                "worktree": "/tmp/nw-wt", "worker": {"state": "idle"},
                "gatekeeper_session_id": "", "cogship_id": "",
                "pending_audience": False, "ui_review": status,
            }]
        }
        policy = nw.Policy(max_workers=2, max_spawns_per_tick=2, max_spawns_per_hour=10)
        actions, notes = nw.plan(
            snap, {}, policy,
            {"count": 0, "rss_mb": 0.0, "by_role": {}, "workers": [], "dirs": {}},
            ui_pending_ids=["Q9901"], atelier_ready=["Q9901"],
        )
        keys = {n.get("key") for n in notes}
        self.assertIn("atelier_pending", keys)
        self.assertEqual([a for a in actions if a.kind == "collect"], [])
        atelier_actions = [a for a in actions if a.kind == "atelier"]
        self.assertEqual(len(atelier_actions), 1)
        self.assertEqual(atelier_actions[0].quest_ids, ["Q9901"])

    def test_planner_stands_down_for_a_live_artist_turn(self):
        policy = nw.Policy(max_workers=2, max_spawns_per_tick=2, max_spawns_per_hour=10)
        snap = {"quests": [{
            "id": "Q9901", "kind": "quest", "status": "GATE",
            "worktree": "/tmp/nw-wt", "worker": {"state": "idle"},
            "gatekeeper_session_id": "", "cogship_id": "",
            "pending_audience": False, "ui_review": "PENDING",
        }]}
        actions, notes = nw.plan(
            snap, {}, policy,
            {"count": 0, "rss_mb": 0.0, "by_role": {}, "workers": [], "dirs": {}},
            ui_pending_ids=["Q9901"], atelier_ready=["Q9901"], atelier_blocked=True,
        )
        self.assertEqual([a for a in actions if a.kind == "atelier"], [])
        self.assertIn("atelier_artist_live", {n.get("key") for n in notes})

    def test_verify_convoy_blocks_the_same_quest_the_planner_skips(self):
        tmp = self._tmp_with("DEFERRED — close later")
        self.assertTrue(convoy_guard.ui_review_outstanding_for(tmp, "Q9901")[0])

        findings = self._audit(tmp, allow_ui_pending=False)
        blocking = [f for f in findings if f.code == "ui-review-pending"]
        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0].level, "error")
        self.assertIn("atelier", blocking[0].message)

        findings = self._audit(tmp, allow_ui_pending=True)
        self.assertEqual([f for f in findings if f.code == "ui-review-pending"], [])

    def test_predicate_and_gate_agree_when_the_review_is_signed(self):
        tmp = self._tmp_with("APPROVED — signed by M'Lord 2026-11-16")
        self.assertFalse(convoy_guard.ui_review_outstanding_for(tmp, "Q9901")[0])
        findings = self._audit(tmp, allow_ui_pending=False)
        self.assertEqual([f for f in findings if f.code == "ui-review-pending"], [])

    # -- helpers ---------------------------------------------------------

    def _tmp_with(self, ui_review_line):
        import tempfile

        tmp = tempfile.mkdtemp(prefix="q1574-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        _write_charter(tmp, "Q9901", ui_review_line)
        return tmp

    def _audit(self, tmp, allow_ui_pending):
        """Run audit_promotion with the git/pack reads stubbed for one Quest."""
        fake = MagicMock(returncode=1, stdout="", stderr="")
        branch = "quest/q9901-test"
        with patch("court.convoy_guard._git", return_value=fake), \
             patch("court.convoy_guard.convoy_tip", return_value="tipsha"), \
             patch("court.convoy_guard.load_pack",
                   return_value={"quest_ids": ["Q9901"]}), \
             patch("court.convoy_guard.merged_quest_branches",
                   return_value={branch: ["sha1"]}), \
             patch("court.convoy_guard.contained_quest_branches", return_value={}), \
             patch("court.convoy_guard.quest_status", return_value="GATE"), \
             patch("court.convoy_guard.ui_mandate_outstanding", return_value=(False, "")), \
             patch("court.convoy_guard.studio_pending", return_value=(False, "")), \
             patch("court.convoy_guard.suite_proof", return_value={}):
            return convoy_guard.audit_promotion(
                Path(tmp), "cogship-999", allow_ui_pending=allow_ui_pending,
            )


class DefaultAtelierLaneTests(unittest.TestCase):
    """The atelier merge lane runs without an explicit --enable."""

    def test_default_policy_enables_atelier(self):
        self.assertIn("atelier", nw.Policy().enabled)
        self.assertTrue(nw.Policy().allows("atelier"))
        self.assertIn("atelier", nw.ENABLED_ACTIONS)

    def test_cli_default_enable_includes_atelier(self):
        parser = cli.build_parser()
        args = parser.parse_args(["nightwatch", "--once"])
        self.assertIn("atelier", (args.enable or "").split(","))


if __name__ == "__main__":
    unittest.main()
