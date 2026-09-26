"""Steward protocol tests: real-data dry-run gates for data-mutation quests
and the fire-and-forget (never block) session discipline."""
import json
import tempfile
import unittest
from pathlib import Path

from court import config
from court.init_cmd import DEFAULT_KILO_CONFIG

BASE = Path(__file__).resolve().parent.parent


class HarnessCommandConfigTests(unittest.TestCase):
    """harness.command in .court/config.json names the castle's read-only
    production verification harness (pb-app: ROQ scripts/db/roq.py)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.court_dir = Path(self._tmp.name) / ".court"
        self.court_dir.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_when_unconfigured(self):
        self.assertEqual(config.get_harness_command(court_dir=self.court_dir), "")

    def test_resolves_from_manifest(self):
        (self.court_dir / "config.json").write_text(json.dumps({
            "harness": {"command": "ROLE=web python scripts/db/roq.py"},
        }), encoding="utf-8")
        self.assertEqual(
            config.get_harness_command(court_dir=self.court_dir),
            "ROLE=web python scripts/db/roq.py",
        )

    def test_template_ships_structural_harness_block(self):
        tpl = json.loads((BASE / "court" / "templates" / "config.template.json").read_text())
        self.assertIn("harness", tpl)
        self.assertIn("command", tpl["harness"])
        self.assertIn("harness.command", tpl["_manifest_note"])


class StewardProtocolDocTests(unittest.TestCase):
    """The prompting layer must carry both disciplines end to end."""

    def test_steward_workflow_doc_carries_both_protocols(self):
        doc = (BASE / "court" / "prompts" / "steward.md").read_text(encoding="utf-8")
        # Fire-and-forget session discipline
        self.assertIn("Never Block the Session", doc)
        self.assertIn("No sleep timers", doc)
        self.assertIn("Fire and forget", doc)
        self.assertIn("pull a status update", doc)
        # Real-data dry-run gates
        self.assertIn("Data-Mutation Quests Require a Real-Data Dry-Run Gate", doc)
        self.assertIn("harness.command", doc)
        self.assertIn("0.01%", doc)
        self.assertIn("near-zero match rate is a FAIL", doc)

    def test_steward_agent_persona_carries_both_protocols(self):
        doc = (BASE / "court" / "agents" / "steward.md").read_text(encoding="utf-8")
        self.assertIn("Never Block the Session", doc)
        self.assertIn("Real-Data Dry-Run Gate", doc)
        self.assertIn("harness.command", doc)

    def test_kilo_json_steward_and_serf_prompts_carry_protocols(self):
        steward_prompt = DEFAULT_KILO_CONFIG["agent"]["steward"]["prompt"]
        self.assertIn("NEVER BLOCK THE SESSION", steward_prompt)
        self.assertIn("REAL-DATA DRY-RUN GATE", steward_prompt)
        serf_prompt = DEFAULT_KILO_CONFIG["agent"]["serf"]["prompt"]
        self.assertIn("REAL-DATA DRY-RUN EVIDENCE", serf_prompt)
        self.assertIn("pasted evidence", serf_prompt)

    def test_master_of_coin_template_gates_on_dry_run_evidence(self):
        doc = (BASE / "court" / "templates" / "master_of_coin_review_prompt.md").read_text(encoding="utf-8")
        self.assertIn("Data-Mutation Verification Gate", doc)
        self.assertIn("harness.command", doc)
        self.assertIn("near-zero match rate", doc)
        # Checklist numbering stays contiguous after the insertion.
        for n in range(1, 13):
            self.assertIn(f"{n}. **", doc)

    def test_repo_agents_md_steward_bullet(self):
        doc = (BASE / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Never blocks the session", doc)
        self.assertIn("dry-run gate", doc)
        self.assertIn("harness.command", doc)

    def test_charter_command_doc_mentions_dry_run_gate(self):
        doc = (BASE / "court" / "commands" / "charter.md").read_text(encoding="utf-8")
        self.assertIn("Data-Mutation Dry-Run Gate", doc)
        self.assertIn("chartered UP FRONT", doc)
        self.assertIn("Fire-and-forget", doc)


if __name__ == "__main__":
    unittest.main()
