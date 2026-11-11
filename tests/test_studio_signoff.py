"""Tests for the studio "Approve" wiring: branch→quest resolution, token
issuance, and the annotator placeholders court/browser.py bakes in.

Covers the pure helpers behind court/ui_server.py's /api/signoff endpoints and
the browser-side substitution — the endpoint handlers themselves are exercised
through these helpers (an HTTP server round-trip is out of scope here).
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from court import browser, ui_server


def _init_git_repo(repo_dir, branch):
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=str(repo_dir),
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "t@t"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "t"],
                   check=True, capture_output=True)
    (repo_dir / "seed.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "seed"],
                   check=True, capture_output=True)


class TestStudioQuestResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name) / "artist-studio-q617-q627"
        self.wt.mkdir()
        self.git_root = str(self.wt)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolves_quests_from_ui_studio_branch(self):
        _init_git_repo(self.wt, "artist/q617-q627-ui-studio")
        qd = self.wt / ".court" / "quests"
        qd.mkdir(parents=True)
        (qd / "Q617-Some.md").write_text("id: Q617\n", encoding="utf-8")
        (qd / "Q627-Other.md").write_text("id: Q627\n", encoding="utf-8")
        (qd / "Q999-Neighbour.md").write_text("id: Q999\n", encoding="utf-8")
        self.assertEqual(ui_server._studio_quest_ids(str(self.wt)),
                         ["Q617", "Q627"])

    def test_non_studio_branch_resolves_empty(self):
        _init_git_repo(self.wt, "castle")
        self.assertEqual(ui_server._studio_quest_ids(str(self.wt)), [])


class TestAtelierQuestResolution(unittest.TestCase):
    """The standing atelier's members come from the repo-level manifest, not
    from the branch name (its branch is the stable `artist/atelier`)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        _init_git_repo(self.root, "castle")
        self.atelier_wt = self.root / ".kilo" / "worktrees" / "artist-atelier"

    def tearDown(self):
        self.tmp.cleanup()

    def _manifest(self, ids):
        d = self.root / ".court" / "atelier"
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.json").write_text(
            json.dumps({"version": 1, "branch": "artist/atelier",
                        "quests": {i: {"branch": f"quest/{i.lower()}"} for i in ids}}),
            encoding="utf-8")

    def _add_atelier_worktree(self):
        subprocess.run(["git", "-C", str(self.root), "worktree", "add", "-q",
                        str(self.atelier_wt), "-b", "artist/atelier"],
                       check=True, capture_output=True)

    def test_is_atelier_branch(self):
        self.assertTrue(ui_server._is_atelier_branch("artist/atelier"))
        self.assertFalse(ui_server._is_atelier_branch("artist/q617-q627-ui-studio"))
        self.assertFalse(ui_server._is_atelier_branch(""))

    def test_resolves_members_from_the_repo_manifest(self):
        self._manifest(["Q1358-Common-WarehouseScannerUPCScanRate",
                        "Q1403-Marketing-Trigger-Condition-Null-Operator-Inversion"])
        self._add_atelier_worktree()
        # Read from the MAIN checkout's manifest, though we query the worktree.
        self.assertEqual(ui_server._studio_quest_ids(str(self.atelier_wt)),
                         ["Q1358-Common-WarehouseScannerUPCScanRate",
                          "Q1403-Marketing-Trigger-Condition-Null-Operator-Inversion"])
        ok, err = ui_server._check_studio_signoff_ready(str(self.atelier_wt))
        self.assertTrue(ok, err)

    def test_atelier_without_a_manifest_resolves_empty(self):
        self._add_atelier_worktree()
        self.assertEqual(ui_server._studio_quest_ids(str(self.atelier_wt)), [])


class TestSignoffToken(unittest.TestCase):
    def test_token_is_stable_per_worktree(self):
        key = "/tmp/some-studio-wt"
        ui_server._SIGNOFF_TOKENS.clear()
        t1 = ui_server._signoff_token(key)
        t2 = ui_server._signoff_token(key)
        self.assertTrue(t1)
        self.assertEqual(t1, t2)
        self.assertTrue(len(t1) >= 32)
        ui_server._SIGNOFF_TOKENS.clear()


class TestAnnotatorPlaceholders(unittest.TestCase):
    def test_signoff_placeholders_substituted(self):
        src = browser.load_annotator_source(
            "http://127.0.0.1:8300/api/annotation",
            "/wt",
            signoff_token="sekrit",
        )
        self.assertIn("var SIGNOFF_TOKEN = \"sekrit\";", src)
        self.assertIn("var SIGNOFF_ENDPOINT = \"http://127.0.0.1:8300/api/signoff\";",
                      src)
        self.assertNotIn("{{SIGNOFF", src)
        self.assertNotIn("{{WORKTREE}}", src)

    def test_empty_token_disarms_approve(self):
        src = browser.load_annotator_source(
            "http://127.0.0.1:8300/api/annotation", "/wt")
        self.assertIn("var SIGNOFF_TOKEN = \"\";", src)


if __name__ == "__main__":
    unittest.main()