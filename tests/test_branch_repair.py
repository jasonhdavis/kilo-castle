"""
Unit tests for branch folder nesting, canonical name resolution, CLI fix-branches,
and worktree checked-out branch auditing.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from court import branch_ops, git_ops, store, ward
from court.models import Quest, validate_branch_name


class TestBranchNameValidation(unittest.TestCase):
    def test_validate_branch_name_valid(self):
        valid_branches = [
            "main",
            "castle",
            "the-gatehouse/north",
            "the-gatehouse/south",
            "the-gatehouse/east",
            "the-gatehouse/west",
            "the-gatehouse/cogship-042",
            "epic/q050-platform-task-execution-v2-api-security",
            "quest/q084-platform-local-pg-r2-sync-and-neon-cutoff",
            "quest/q014/q088-crm-prospect-geocluster-resolution",
            "scout/q074-shops-product-runbook-architecture-and-workflow-spike",
            "scout/q013/q005-marketplace-retailer-fingerprint",
        ]
        for b in valid_branches:
            is_valid, err = validate_branch_name(b)
            self.assertTrue(is_valid, f"Expected '{b}' to be valid, got error: {err}")
            self.assertEqual(err, "")

    def test_validate_branch_name_invalid_flat(self):
        invalid_flat_branches = [
            "quest-q084-platform-local-pg-r2-sync-and-neon-cuto",
            "quest-q014-q088-crm-prospect-geocluster-resolution",
            "scout-q074-shops-product-runbook-architecture-and-workflow-spike",
            "epic-q012-intelligence-forecasting",
        ]
        for b in invalid_flat_branches:
            is_valid, err = validate_branch_name(b)
            self.assertFalse(is_valid, f"Expected '{b}' to be invalid flat branch")
            self.assertIn("violates folder hierarchy", err)


class TestCanonicalBranchResolution(unittest.TestCase):
    def test_resolve_trunks(self):
        for trunk in ("main", "castle"):
            can, reason = branch_ops.resolve_canonical_branch_name(trunk)
            self.assertEqual(can, trunk)
            self.assertEqual(reason, "trunk")

    def test_resolve_gatehouse(self):
        for station in ("north", "south", "east", "west"):
            flat = f"the-gatehouse-{station}"
            can, reason = branch_ops.resolve_canonical_branch_name(flat)
            self.assertEqual(can, f"the-gatehouse/{station}")
            self.assertEqual(reason, "gatehouse_flat_to_slash")

    def test_resolve_fallback_regex(self):
        flat = "quest-q999-my-hypothetical-feature"
        can, reason = branch_ops.resolve_canonical_branch_name(flat)
        self.assertEqual(can, "quest/q999-my-hypothetical-feature")
        self.assertEqual(reason, "regex_standalone_quest")

        flat_child = "quest-q900-q901-sub-feature"
        can, reason = branch_ops.resolve_canonical_branch_name(flat_child)
        self.assertEqual(can, "quest/q900/q901-sub-feature")
        self.assertEqual(reason, "regex_child_quest")


class TestWorktreeBranchAuditing(unittest.TestCase):
    def test_ward_flags_branch_mismatch(self):
        quest = Quest(
            id="Q990-Test-Quest",
            title="Test Quest",
            branch="quest/q990-test-quest",
            status="WORKING",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            subprocess.run(["git", "init", "-b", "quest-q990-test-quest"], cwd=tmp_dir, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Serf"], cwd=tmp_dir, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "serf@court.local"], cwd=tmp_dir, capture_output=True, check=True)
            (tmp_path / "dummy.txt").write_text("hello")
            subprocess.run(["git", "add", "dummy.txt"], cwd=tmp_dir, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_dir, capture_output=True, check=True)

            audit_res = ward.audit_quest(quest, worktree_path=tmp_path)
            self.assertFalse(audit_res.is_compliant)
            mismatch_violation = any("branch mismatch" in str(v).lower() for v in audit_res.violations)
            self.assertTrue(mismatch_violation, f"Expected branch mismatch violation, got: {audit_res.violations}")

    def test_ward_accepts_matching_canonical_slash_branch(self):
        quest = Quest(
            id="Q991-Test-Quest",
            title="Test Quest",
            branch="quest/q991-test-quest",
            status="WORKING",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            subprocess.run(["git", "init", "-b", "quest/q991-test-quest"], cwd=tmp_dir, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Serf"], cwd=tmp_dir, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "serf@court.local"], cwd=tmp_dir, capture_output=True, check=True)
            (tmp_path / "dummy.txt").write_text("hello")
            subprocess.run(["git", "add", "dummy.txt"], cwd=tmp_dir, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_dir, capture_output=True, check=True)

            audit_res = ward.audit_quest(quest, worktree_path=tmp_path)
            mismatch_violation = any("branch mismatch" in str(v).lower() for v in audit_res.violations)
            self.assertFalse(mismatch_violation, f"Unexpected branch mismatch violation: {audit_res.violations}")


if __name__ == "__main__":
    unittest.main()
