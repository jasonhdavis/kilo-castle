import unittest
from court import store
from court.models import Quest


class TestCommutationAndAudience(unittest.TestCase):
    def test_extract_commutation_from_moc_bullet(self):
        q = Quest(
            id="Q950",
            title="Test Commutation Quest",
            app="orders",
            concern="test",
            body_sections={
                "Master of Coin's Audit": "- **Verdict:** PASS\n- **Commutation:** Run migrations and backfill cache.\n- **Recommended Next Steps:** Ship it.",
            },
        )
        self.assertEqual(q.extract_commutation(), "Run migrations and backfill cache.")

    def test_extract_commutation_none(self):
        q = Quest(
            id="Q951",
            title="Test No Commutation Quest",
            app="common",
            concern="test",
            body_sections={
                "Master of Coin's Audit": "- **Verdict:** PASS\n- **Commutation:** None.\n- **Recommended Next Steps:** Ship it.",
            },
        )
        self.assertEqual(q.extract_commutation(), "")

    def test_extract_audience_and_pending(self):
        # 1. No audience required
        q1 = Quest(
            id="Q952",
            title="Test Audience Quest 1",
            app="crm",
            concern="test",
            body_sections={
                "Tribute Rendered": "## Audience\nNone required.\n\n## Ballad\nDone.",
            },
        )
        self.assertEqual(q1.extract_audience(), "None required.")
        self.assertFalse(q1.has_pending_audience())

        # 2. Substantive audience without log resolution
        q2 = Quest(
            id="Q953",
            title="Test Audience Quest 2",
            app="crm",
            concern="test",
            body_sections={
                "Tribute Rendered": "## Audience\nRequires M'Lord to decide whether to deprecate legacy endpoint v1.\n\n## Ballad\nDone.",
            },
        )
        self.assertTrue(q2.has_pending_audience())

        # 3. Substantive audience with log resolution
        q3 = Quest(
            id="Q954",
            title="Test Audience Quest 3",
            app="crm",
            concern="test",
            body_sections={
                "Tribute Rendered": "## Audience\nRequires M'Lord to decide whether to deprecate legacy endpoint v1.\n\n## Ballad\nDone.",
                "Audience Log": "### 2026-09-06: M'Lord approved deprecating v1.",
            },
        )
        self.assertFalse(q3.has_pending_audience())

    def test_rollup_ship_manifest_commutations(self):
        q1 = Quest(
            id="Q955",
            title="Quest With Commutation",
            app="platform",
            concern="test",
            status="READY_TO_RAZE",
            body_sections={
                "Master of Coin's Audit": "- **Verdict:** PASS\n- **Commutation:** Deploy worker fleet and toggle setting.\n- **Recommended Next Steps:** Ship.",
                "Tribute Rendered": "## Ballad\nGood work.",
            },
        )
        manifest = store.rollup_ship_manifest(quests=[q1])
        self.assertEqual(len(manifest["commutations"]), 1)
        self.assertEqual(manifest["commutations"][0][0].id, "Q955")
        self.assertEqual(manifest["commutations"][0][1], "Deploy worker fleet and toggle setting.")
        self.assertEqual(manifest["commutations_done"], [])

    def test_commutation_complete_flow(self):
        q = Quest(
            id="Q956",
            title="Commutation Completion Flow",
            app="platform",
            concern="test",
            status="READY_TO_RAZE",
            body_sections={
                "Master of Coin's Audit": "- **Verdict:** PASS\n- **Commutation:** Deploy worker fleet and toggle setting.\n- **Recommended Next Steps:** Ship.",
                "Cogship Log": "- **Result:** PASS\n- **Promoted Commit:** abc123",
            },
        )
        self.assertTrue(q.commutation_required())
        self.assertFalse(q.commutation_complete())
        self.assertEqual(q.commutation_log_entries(), [])

        entry = q.append_commutation("Deployed worker fleet and toggled setting.")
        self.assertTrue(entry.startswith("- **Commutation ("))
        self.assertTrue(q.commutation_complete())
        self.assertEqual(len(q.commutation_log_entries()), 1)
        self.assertIn("Commutation", q.body_sections["Cogship Log"])

    def test_commutation_not_required_never_done(self):
        q = Quest(
            id="Q957",
            title="No Commutation Quest",
            app="common",
            concern="test",
            body_sections={
                "Master of Coin's Audit": "- **Verdict:** PASS\n- **Commutation:** None.\n- **Recommended Next Steps:** Ship.",
                "Cogship Log": "- **Result:** PASS",
            },
        )
        self.assertFalse(q.commutation_required())
        self.assertFalse(q.commutation_complete())

    def test_commutation_handwritten_variant_recognized(self):
        q = Quest(
            id="Q958",
            title="Handwritten Commutation Marker",
            app="marketplace_intelligence",
            concern="test",
            body_sections={
                "Master of Coin's Audit": "- **Commutation:** 1. Set env var BRAVE_SEARCH_API_KEY_FREE in Fly secrets. 2. Verify ledger table.\n",
                "Cogship Log": "- **Commutation #1 (2026-09-08):** Set env var BRAVE_SEARCH_API_KEY_FREE on pb-app and pb-app-worker; verified ledger table via brave_search_usage ✓",
            },
        )
        self.assertTrue(q.commutation_complete())
        self.assertEqual(len(q.commutation_log_entries()), 1)

    def test_rollup_ship_manifest_splits_done_commutations(self):
        pending = Quest(
            id="Q959",
            title="Pending Commutation",
            app="platform",
            concern="test",
            status="READY_TO_RAZE",
            body_sections={"Master of Coin's Audit": "- **Commutation:** Toggle worker setting."},
        )
        done = Quest(
            id="Q960",
            title="Done Commutation",
            app="platform",
            concern="test",
            status="READY_TO_RAZE",
            body_sections={
                "Master of Coin's Audit": "- **Commutation:** Toggle worker setting.",
                "Cogship Log": "- **Commutation (2026-09-08):** Toggled worker setting.",
            },
        )
        manifest = store.rollup_ship_manifest(quests=[pending, done])
        self.assertEqual([q.id for q, _ in manifest["commutations"]], ["Q959"])
        self.assertEqual([q.id for q, _ in manifest["commutations_done"]], ["Q960"])


if __name__ == "__main__":
    unittest.main()
