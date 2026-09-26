"""Q455/Q432 engine tests ported from the pb-app castle: manifest-driven role
config, migration-graph preflight (module + ship preflight), and the runsuite
graph stage. Stdlib-only: the Django subprocess is stubbed throughout."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from court import cli, config, git_ops, migration_graph


class CourtManifestConfigTests(unittest.TestCase):
    """Q455: .court/config.json is the single source of truth for role models."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.court_dir = Path(self._tmp.name) / ".court"
        self.court_dir.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_role_is_a_loud_error(self):
        with self.assertRaises(config.CourtConfigError):
            config.get_model("gatekeeper", court_dir=self.court_dir)

    def test_alias_resolution_reads_manifest(self):
        (self.court_dir / "config.json").write_text(json.dumps({
            "models": {"serf": "GLM-5.3-Flash"},
            "model_aliases": {"GLM-5.3-Flash": "openrouter/z-ai/glm-5.3-flash"},
        }), encoding="utf-8")
        self.assertEqual(
            config.canonical_model_id("GLM-5.3-Flash", court_dir=self.court_dir),
            "openrouter/z-ai/glm-5.3-flash",
        )
        # Qualified IDs pass through untouched.
        self.assertEqual(
            config.canonical_model_id("openrouter/z-ai/glm-5.3", court_dir=self.court_dir),
            "openrouter/z-ai/glm-5.3",
        )

    def test_suite_command_from_manifest(self):
        self.assertEqual(config.get_suite_command(court_dir=self.court_dir), "")
        (self.court_dir / "config.json").write_text(json.dumps({
            "suite": {"command": "python3 -m pytest tests -q"},
        }), encoding="utf-8")
        self.assertEqual(
            config.get_suite_command(court_dir=self.court_dir),
            "python3 -m pytest tests -q",
        )


class CourtMigrationGraphPreflightTests(unittest.TestCase):
    """Q432 (2026-09-15): migration-graph integrity preflight in engine code.

    Background: the 2026-09-14 v1383 deploy failed at release_command because
    two same-parent 0130 migrations existed in `common` (parallel quests
    Q391/Q401) — a fork invisible to git (differently-named files merge
    cleanly) and to per-quest test suites (each sees only its own leaf). The
    guard runs `ROLE=web python manage.py makemigrations --check --dry-run`
    (DB-free graph build) in `court ship` preflight and as stage 1 of
    `court runsuite`. These tests are stdlib-only: the Django subprocess is
    stubbed, no live Django/DB dependency.
    """

    # The real Django 5.x CommandError text, as emitted by the v1383 incident
    # state (two same-parent 0130 leaves in `common`).
    FORK_STDERR = (
        "CommandError: Conflicting migrations detected; multiple leaf nodes in "
        "the migration graph: (0130_marketplacealias_shopify_available_quantity_and_more, "
        "0130_marketplacealias_vendor in common).\n"
        "To fix them run 'python manage.py makemigrations --merge'\n"
    )

    def _make_checkout(self):
        """A minimal fake checkout: a temp dir with a manage.py file."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        (tmp / "manage.py").write_text("# stub\n")
        return tmp

    def _stub_makemigrations(self, exit_code, stdout="", stderr=""):
        """Patch court.migration_graph subprocess + venv discovery so
        check_migration_graph observes a stubbed makemigrations run."""

        def fake_run(argv, cwd=None, env=None, capture_output=True, text=True, timeout=None):
            return subprocess.CompletedProcess(argv, exit_code, stdout=stdout, stderr=stderr)

        return (
            patch("court.migration_graph.subprocess.run", side_effect=fake_run),
            patch("court.migration_graph._find_parent_venv_python", return_value=None),
        )

    # ---------------------- check_migration_graph -------------------------

    def test_single_leaf_clean_graph_passes(self):
        checkout = self._make_checkout()
        p_patch, v_patch = self._stub_makemigrations(0, stdout="No changes detected\n", stderr="")
        with p_patch, v_patch:
            res = migration_graph.check_migration_graph(checkout)
        self.assertTrue(res["ok"])
        self.assertEqual(res["mode"], "clean")
        self.assertEqual(res["offenders"], [])
        self.assertIsNone(res["reason"])
        self.assertEqual(res["exit_code"], 0)

    def test_multiple_leaf_graph_fails_with_named_leaves(self):
        checkout = self._make_checkout()
        p_patch, v_patch = self._stub_makemigrations(1, stdout="", stderr=self.FORK_STDERR)
        with p_patch, v_patch:
            res = migration_graph.check_migration_graph(checkout)
        self.assertFalse(res["ok"])
        self.assertEqual(res["mode"], "multiple_leaf_nodes")
        self.assertEqual(
            res["offenders"],
            [{
                "app": "common",
                "leaves": [
                    "0130_marketplacealias_shopify_available_quantity_and_more",
                    "0130_marketplacealias_vendor",
                ],
            }],
        )
        # The failure message must name the offending app and the leaf migrations.
        self.assertIn("common", res["reason"])
        self.assertIn("0130_marketplacealias_shopify_available_quantity_and_more", res["reason"])
        self.assertIn("0130_marketplacealias_vendor", res["reason"])

    def test_multi_app_conflict_parses_each_app(self):
        output = (
            "CommandError: Conflicting migrations detected; multiple leaf nodes in "
            "the migration graph: (0054_x, 0054_y in crm; 0130_a in common)."
        )
        offenders, mode = migration_graph._parse_offenders(1, output)
        self.assertEqual(mode, "multiple_leaf_nodes")
        self.assertEqual(
            offenders,
            [
                {"app": "crm", "leaves": ["0054_x", "0054_y"]},
                {"app": "common", "leaves": ["0130_a"]},
            ],
        )

    def test_missing_migration_state_fails(self):
        checkout = self._make_checkout()
        stdout = (
            "Migrations for 'inventory':\n"
            "  apps/inventory/migrations/0037_alter_inventoryadjustments_adjustment_type.py\n"
            "    ~ Alter field adjustment_type on inventoryadjustments\n"
        )
        p_patch, v_patch = self._stub_makemigrations(1, stdout=stdout, stderr="")
        with p_patch, v_patch:
            res = migration_graph.check_migration_graph(checkout)
        self.assertFalse(res["ok"])
        self.assertEqual(res["mode"], "missing_migrations")
        self.assertEqual(res["offenders"], [{"app": "inventory", "leaves": []}])
        self.assertIn("inventory", res["reason"])

    def test_environment_failure_fails_closed_but_distinct(self):
        """A settings/env failure (e.g. missing .env in a fresh worktree) is
        not a graph verdict — but at a deploy gate an unverifiable graph must
        fail closed, with a message that says the graph was NOT verified."""
        checkout = self._make_checkout()
        stderr = "ValueError: QSTASH_URL environment variable is required\n"
        p_patch, v_patch = self._stub_makemigrations(1, stdout="", stderr=stderr)
        with p_patch, v_patch:
            res = migration_graph.check_migration_graph(checkout)
        self.assertFalse(res["ok"])
        self.assertEqual(res["mode"], "environment")
        self.assertEqual(res["offenders"], [])
        self.assertIn("could not be verified", res["reason"])

    def test_unknown_failure_fails_closed(self):
        checkout = self._make_checkout()
        p_patch, v_patch = self._stub_makemigrations(1, stdout="", stderr="boom\n")
        with p_patch, v_patch:
            res = migration_graph.check_migration_graph(checkout)
        self.assertFalse(res["ok"])
        self.assertEqual(res["mode"], "unknown")

    def test_command_builder_prefers_chartered_shell_form_with_local_venv(self):
        checkout = self._make_checkout()
        (checkout / "venv" / "bin").mkdir(parents=True)
        (checkout / "venv" / "bin" / "activate").write_text("# stub\n")
        argv, _env = migration_graph.build_migration_graph_command(checkout)
        self.assertEqual(argv[0], "bash")
        self.assertEqual(argv[1], "-c")
        self.assertEqual(argv[2], migration_graph.MIGRATION_GRAPH_SHELL_COMMAND)
        self.assertIn("makemigrations --check --dry-run", argv[2])
        self.assertIn("ROLE=web", argv[2])

    def test_command_builder_falls_back_without_local_venv(self):
        """Quest/gatehouse worktrees carry no venv — the check must still run
        via the parent checkout's venv interpreter (argv form)."""
        checkout = self._make_checkout()  # no venv/
        with patch("court.migration_graph._find_parent_venv_python",
                   return_value="/repo/venv/bin/python"):
            argv, env = migration_graph.build_migration_graph_command(checkout)
        self.assertEqual(argv, ["/repo/venv/bin/python"] + migration_graph.MIGRATION_GRAPH_ARGV_COMMAND)
        self.assertEqual(env["ROLE"], "web")

    def test_missing_manage_py_is_fail_closed(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        res = migration_graph.check_migration_graph(tmp)
        self.assertFalse(res["ok"])
        self.assertIn("cannot run", res["reason"])

    # ---------------------- court ship preflight --------------------------

    def _stub_ship_git_ok(self):
        """All git preflight checks answer green."""

        def side_effect(cmd, cwd, timeout=60):
            cmd_str = " ".join(cmd)
            if "rev-parse --abbrev-ref" in cmd_str:
                return {"ok": True, "stdout": "main"}
            if "status --porcelain" in cmd_str:
                return {"ok": True, "stdout": ""}
            if "rev-parse --verify" in cmd_str:
                return {"ok": True, "stdout": "sha"}
            if "fetch" in cmd_str:
                return {"ok": True, "stdout": ""}
            if "rev-list --count" in cmd_str:
                return {"ok": True, "stdout": "0"}
            return {"ok": True, "stdout": ""}

        return patch("court.git_ops._run", side_effect=side_effect)

    def test_ship_preflight_blocks_on_forked_migration_graph(self):
        main_wt = self._make_checkout()
        graph_fail = {
            "ok": False,
            "mode": "multiple_leaf_nodes",
            "offenders": [{"app": "common", "leaves": ["0130_a", "0130_b"]}],
            "reason": "migration graph has multiple leaf nodes — app 'common': "
                      "leaf migrations 0130_a, 0130_b",
            "output_tail": "CommandError: Conflicting migrations detected",
        }
        with self._stub_ship_git_ok(), \
             patch("court.migration_graph.check_migration_graph",
                   return_value=graph_fail) as mock_graph:
            ok, problems = cli._ship_preflight(main_wt, "main", "castle")
        self.assertFalse(ok)
        mock_graph.assert_called_once_with(main_wt)
        joined = "\n".join(problems)
        self.assertIn("migration graph preflight failed", joined)
        self.assertIn("common", joined)
        self.assertIn("0130_a", joined)

    def test_ship_preflight_clean_graph_reports_no_problem(self):
        main_wt = self._make_checkout()
        graph_ok = {"ok": True, "mode": "clean", "offenders": [], "reason": None, "output_tail": ""}
        with self._stub_ship_git_ok(), \
             patch("court.migration_graph.check_migration_graph", return_value=graph_ok):
            ok, problems = cli._ship_preflight(main_wt, "main", "castle")
        self.assertTrue(ok)
        self.assertEqual(problems, [])

    # ---------------------- court runsuite proof stamp --------------------

    def _proof_path(self, tmp):
        return tmp / "proof.json"

    def test_run_unified_suite_graph_failure_fails_fast_and_stamps_proof(self):
        """Graph failure => exit code 2, migration_graph_ok=False stamped in
        the proof JSON, and the expensive test battery is never started."""
        wt = self._make_checkout()
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        proof_path = self._proof_path(tmp)
        graph_fail = {
            "ok": False,
            "exit_code": 1,
            "command": migration_graph.MIGRATION_GRAPH_SHELL_COMMAND,
            "mode": "multiple_leaf_nodes",
            "offenders": [{"app": "common", "leaves": ["0130_a", "0130_b"]}],
            "reason": "migration graph has multiple leaf nodes — app 'common'",
            "output_tail": "CommandError: Conflicting migrations",
        }
        with patch("court.git_ops._run", return_value={"ok": True, "stdout": "abc123"}), \
             patch("court.migration_graph.check_migration_graph", return_value=graph_fail), \
             patch("court.git_ops.subprocess.run") as mock_suite_run, \
             patch("court.git_ops._suite_proof_path", return_value=proof_path):
            res = git_ops.run_unified_suite(wt, cogship_id="cogship-q432-test")

        self.assertFalse(res["ok"])
        self.assertEqual(res["exit_code"], 2)
        self.assertFalse(res["migration_graph_ok"])
        self.assertIn("migration graph preflight failed", res["error"])
        # Fail fast: the test battery must not run on a broken graph.
        mock_suite_run.assert_not_called()
        proof = json.loads(proof_path.read_text())
        self.assertFalse(proof["migration_graph_ok"])
        self.assertEqual(proof["exit_code"], 2)
        self.assertEqual(proof["migration_graph_mode"], "multiple_leaf_nodes")

    def test_run_unified_suite_graph_pass_stamps_field_and_runs_suite(self):
        wt = self._make_checkout()
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        proof_path = self._proof_path(tmp)
        graph_ok = {"ok": True, "exit_code": 0, "mode": "clean", "offenders": [],
                    "reason": None, "output_tail": ""}

        def fake_suite_run(argv, cwd=None, capture_output=True, text=True, timeout=None):
            return subprocess.CompletedProcess(argv, 0, stdout="Ran 241 tests in 235.1s\nOK\n", stderr="")

        with patch("court.git_ops._run", return_value={"ok": True, "stdout": "abc123"}), \
             patch("court.migration_graph.check_migration_graph", return_value=graph_ok), \
             patch("court.git_ops.subprocess.run", side_effect=fake_suite_run), \
             patch("court.git_ops._suite_proof_path", return_value=proof_path):
            res = git_ops.run_unified_suite(wt, cogship_id="cogship-q432-test")

        self.assertTrue(res["ok"])
        self.assertEqual(res["exit_code"], 0)
        self.assertTrue(res["migration_graph_ok"])
        self.assertEqual(res["ran_tests"], 241)
        proof = json.loads(proof_path.read_text())
        self.assertTrue(proof["migration_graph_ok"])
        self.assertEqual(proof["ran_tests"], 241)
        self.assertEqual(proof["exit_code"], 0)

    def test_run_unified_suite_skips_graph_stage_for_non_django_castles(self):
        """A checkout without manage.py has no migration graph: the stage is
        not applicable (stamped None) and the suite battery still runs."""
        wt = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(wt, ignore_errors=True))
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        proof_path = self._proof_path(tmp)

        def fake_suite_run(argv, cwd=None, capture_output=True, text=True, timeout=None):
            return subprocess.CompletedProcess(argv, 0, stdout="3 passed in 0.1s\n", stderr="")

        with patch("court.git_ops._run", return_value={"ok": True, "stdout": "abc123"}), \
             patch("court.migration_graph.check_migration_graph") as mock_graph, \
             patch("court.git_ops.subprocess.run", side_effect=fake_suite_run), \
             patch("court.git_ops._suite_proof_path", return_value=proof_path):
            res = git_ops.run_unified_suite(wt, command="python3 -m pytest -q", cogship_id="cogship-q432-test")

        self.assertTrue(res["ok"])
        self.assertIsNone(res["migration_graph_ok"])
        mock_graph.assert_not_called()
        proof = json.loads(proof_path.read_text())
        self.assertIsNone(proof["migration_graph_ok"])


if __name__ == "__main__":
    unittest.main()
