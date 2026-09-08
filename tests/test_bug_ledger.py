"""Unit tests for the Warden's Quested Bug Ledger (court.bug_ledger)."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from court import bug_ledger, store
from court.models import Quest


class TestBugLedgerModelsAndMatching(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.ledger_file = self.tmp_path / "BUG_LEDGER.md"

    def tearDown(self):
        self._tmp.cleanup()

    def test_bug_record_roundtrip_serialization(self):
        rec1 = bug_ledger.BugRecord(
            bug_id="BUG-PB-APP-23J",
            quest_id="Q211-Inventory-Pb-App-23j-Submit-Fbm-Inventory-Feed-Failed-Feedsv",
            sentry_short_id="PB-APP-23J",
            sentry_issue_id="7716033649",
            sentry_url="https://picobarn.sentry.io/issues/7716033649/",
            app="inventory",
            error_type="TypeError",
            logger="apps.inventory.amazon_fba_service",
            signature="create_feed() missing 2 required positional arguments",
            quest_status="TRIBUTE_READY",
            deploy_status="IN_FLIGHT",
            cogship_id="cogship-019",
            deployed_at="",
            deployed_commit="",
            last_seen_production="2026-09-07T02:15:00Z",
            sentry_status="unresolved",
            created_at="2026-09-07T01:41:01Z",
            updated_at="2026-09-07T02:12:16Z",
            notes="Amazon FBA FeedsV20210630 positional kwargs bug",
        )

        rec2 = bug_ledger.BugRecord(
            bug_id="BUG-PB-APP-A1",
            quest_id="Q152-Platform-Pb-App-A1-Integrityerror-Duplicate-Key-Value-Viola",
            sentry_short_id="PB-APP-A1",
            sentry_issue_id="7710897549",
            sentry_url="https://picobarn.sentry.io/issues/7710897549/",
            app="platform",
            error_type="IntegrityError",
            logger="django.db.backends",
            signature="duplicate key value violates unique constraint",
            quest_status="READY_TO_RAZE",
            deploy_status="DEPLOYED",
            cogship_id="cogship-019",
            deployed_at="2026-09-07T06:00:00Z",
            deployed_commit="abc1234",
            last_seen_production="2026-09-07T05:50:00Z",
            sentry_status="resolved",
            created_at="2026-09-06T12:00:00Z",
            updated_at="2026-09-07T06:00:00Z",
            notes="Migration DDL race",
        )

        bug_ledger.save_bug_ledger([rec1, rec2], path=self.ledger_file)
        self.assertTrue(self.ledger_file.exists())

        loaded = bug_ledger.load_bug_ledger(path=self.ledger_file)
        self.assertEqual(len(loaded), 2)

        # Check rec1
        l1 = [r for r in loaded if r.bug_id == "BUG-PB-APP-23J"][0]
        self.assertEqual(l1.quest_id, rec1.quest_id)
        self.assertEqual(l1.sentry_short_id, "PB-APP-23J")
        self.assertEqual(l1.deploy_status, "IN_FLIGHT")
        self.assertEqual(l1.quest_status, "TRIBUTE_READY")
        self.assertEqual(l1.cogship_id, "cogship-019")
        self.assertTrue(l1.is_in_flight)
        self.assertFalse(l1.is_deployed)

        # Check rec2
        l2 = [r for r in loaded if r.bug_id == "BUG-PB-APP-A1"][0]
        self.assertEqual(l2.quest_id, rec2.quest_id)
        self.assertEqual(l2.deploy_status, "DEPLOYED")
        self.assertEqual(l2.deployed_at, "2026-09-07T06:00:00Z")
        self.assertEqual(l2.deployed_commit, "abc1234")
        self.assertTrue(l2.is_deployed)
        self.assertFalse(l2.is_in_flight)

    def test_match_bug_by_short_id(self):
        b1 = bug_ledger.BugRecord(bug_id="BUG-1", sentry_short_id="PB-APP-23J", app="inventory")
        b2 = bug_ledger.BugRecord(bug_id="BUG-2", sentry_short_id="PB-APP-A1", app="platform")

        matched = bug_ledger.match_bug([b1, b2], short_id="pb-app-23j")
        self.assertIsNotNone(matched)
        self.assertEqual(matched.bug_id, "BUG-1")

        matched_upper = bug_ledger.match_bug([b1, b2], short_id="PB-APP-A1")
        self.assertIsNotNone(matched_upper)
        self.assertEqual(matched_upper.bug_id, "BUG-2")

        unmatched = bug_ledger.match_bug([b1, b2], short_id="PB-APP-999")
        self.assertIsNone(unmatched)

    def test_match_bug_by_issue_id(self):
        b1 = bug_ledger.BugRecord(bug_id="BUG-1", sentry_issue_id="7716033649")
        matched = bug_ledger.match_bug([b1], issue_id="7716033649")
        self.assertIsNotNone(matched)
        self.assertEqual(matched.bug_id, "BUG-1")

    def test_match_bug_by_logger_and_signature(self):
        b1 = bug_ledger.BugRecord(
            bug_id="BUG-SSL",
            logger="apps.marketplace_intelligence.management.commands.scrape_product_pages",
            signature="SSL connection has been closed unexpectedly",
            error_type="OperationalError",
        )

        matched = bug_ledger.match_bug(
            [b1],
            logger="apps.marketplace_intelligence.management.commands.scrape_product_pages",
            message="OperationalError: SSL connection has been closed unexpectedly in handle()",
        )
        self.assertIsNotNone(matched)
        self.assertEqual(matched.bug_id, "BUG-SSL")


class TestBugClassification(unittest.TestCase):
    def test_untracked_bug(self):
        res = bug_ledger.classify_error(None)
        self.assertEqual(res.verdict, bug_ledger.VERDICT_UNTRACKED_BUG)
        self.assertFalse(res.should_suppress)
        self.assertFalse(res.is_regression)

    def test_in_flight_pre_deploy_noise_suppression(self):
        bug = bug_ledger.BugRecord(
            bug_id="BUG-IN-FLIGHT",
            quest_id="Q211",
            quest_status="WORKING",
            deploy_status="IN_FLIGHT",
        )
        res = bug_ledger.classify_error(bug, event_time_iso="2026-09-07T12:00:00Z")
        self.assertEqual(res.verdict, bug_ledger.VERDICT_PRE_DEPLOY_NOISE)
        self.assertTrue(res.should_suppress)
        self.assertFalse(res.is_regression)
        self.assertIn("Fix has not deployed", res.reason)

    def test_staged_pre_deploy_noise_suppression(self):
        bug = bug_ledger.BugRecord(
            bug_id="BUG-STAGED",
            quest_id="Q152",
            quest_status="GATE",
            deploy_status="STAGED",
            cogship_id="cogship-019",
        )
        res = bug_ledger.classify_error(bug, event_time_iso="2026-09-07T12:00:00Z")
        self.assertEqual(res.verdict, bug_ledger.VERDICT_PRE_DEPLOY_NOISE)
        self.assertTrue(res.should_suppress)
        self.assertFalse(res.is_regression)

    def test_deployed_pre_deploy_remnant_ignored(self):
        bug = bug_ledger.BugRecord(
            bug_id="BUG-DEPLOYED",
            quest_id="Q152",
            quest_status="READY_TO_RAZE",
            deploy_status="DEPLOYED",
            deployed_at="2026-09-07T10:00:00Z",
        )
        # Event occurred before deploy cutover (e.g. 09:30)
        res = bug_ledger.classify_error(bug, event_time_iso="2026-09-07T09:30:00Z")
        self.assertEqual(res.verdict, bug_ledger.VERDICT_PRE_DEPLOY_REMNANT)
        self.assertTrue(res.should_suppress)
        self.assertFalse(res.is_regression)

    def test_deployed_post_deploy_regression_alert(self):
        bug = bug_ledger.BugRecord(
            bug_id="BUG-DEPLOYED",
            quest_id="Q152",
            quest_status="READY_TO_RAZE",
            deploy_status="DEPLOYED",
            deployed_at="2026-09-07T10:00:00Z",
            deployed_commit="deadbeef",
        )
        # Event occurred after deploy cutover (e.g. 10:30)
        res = bug_ledger.classify_error(bug, event_time_iso="2026-09-07T10:30:00Z")
        self.assertEqual(res.verdict, bug_ledger.VERDICT_POST_DEPLOY_REGRESSION)
        self.assertFalse(res.should_suppress)
        self.assertTrue(res.is_regression)
        self.assertIn("AFTER deployment cutover", res.reason)

    def test_closed_bug_regression(self):
        bug = bug_ledger.BugRecord(
            bug_id="BUG-CLOSED",
            quest_id="Q100",
            deploy_status="VERIFIED_CLOSED",
        )
        res = bug_ledger.classify_error(bug, event_time_iso="2026-09-07T12:00:00Z")
        self.assertEqual(res.verdict, bug_ledger.VERDICT_REGRESSION_CLOSED)
        self.assertFalse(res.should_suppress)
        self.assertTrue(res.is_regression)


class TestBugLedgerLifecycleAndSync(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.ledger_file = self.tmp_path / "BUG_LEDGER.md"

    def tearDown(self):
        self._tmp.cleanup()

    def test_sync_from_quests(self):
        q1 = Quest(
            id="Q300-Inventory-Pb-App-99a-Test-Bug",
            title="Fix Sentry [PB-APP-99A]: Inventory sync error",
            app="inventory",
            concern="pb-app-99a-test-bug",
            section="Bug fix",
            tags="Bug fix, sentry, PB-APP-99A",
            status="WORKING",
        )
        q1.set_section("The Kingdom Requires", "- **Sentry Issue ID**: 12345678\n- **Permalink**: https://sentry.io/issues/12345678/\n- **Logger**: `apps.inventory.service`\n")

        q2 = Quest(
            id="Q301-Platform-Feature",
            title="Net new platform feature",
            app="platform",
            concern="feature",
            section="Feature",
            status="OPEN",
        )

        created, updated = bug_ledger.sync_from_quests([q1, q2], path=self.ledger_file)
        self.assertEqual(created, 1)
        self.assertEqual(updated, 0)

        records = bug_ledger.load_bug_ledger(path=self.ledger_file)
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r.bug_id, "BUG-PB-APP-99A")
        self.assertEqual(r.sentry_short_id, "PB-APP-99A")
        self.assertEqual(r.sentry_issue_id, "12345678")
        self.assertEqual(r.logger, "apps.inventory.service")
        self.assertEqual(r.deploy_status, "IN_FLIGHT")

    def test_mark_convoy_deployed(self):
        rec = bug_ledger.BugRecord(
            bug_id="BUG-CONVOY",
            quest_id="Q300",
            deploy_status="STAGED",
            cogship_id="cogship-099",
        )
        bug_ledger.save_bug_ledger([rec], path=self.ledger_file)

        deployed = bug_ledger.mark_convoy_deployed(
            cogship_id="cogship-099",
            quest_ids=["Q300"],
            deployed_commit="feedface",
            deployed_at="2026-09-07T12:00:00Z",
            path=self.ledger_file,
        )

        self.assertEqual(len(deployed), 1)
        self.assertEqual(deployed[0].deploy_status, "DEPLOYED")
        self.assertEqual(deployed[0].deployed_at, "2026-09-07T12:00:00Z")
        self.assertEqual(deployed[0].deployed_commit, "feedface")

        reloaded = bug_ledger.load_bug_ledger(path=self.ledger_file)
        self.assertEqual(reloaded[0].deploy_status, "DEPLOYED")


if __name__ == "__main__":
    unittest.main()
