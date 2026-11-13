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
    """The standing atelier's members come from the repo-level manifest, but a
    manifest entry outlives an atelier rebuild; membership is verified against
    the live branch and the main checkout's charters."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        _init_git_repo(self.root, "castle")
        self.atelier_wt = self.root / ".kilo" / "worktrees" / "artist-atelier"

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args, cwd=None):
        subprocess.run(["git", "-C", str(cwd or self.root), *args],
                       check=True, capture_output=True)

    def _manifest(self, ids, stale=()):
        d = self.root / ".court" / "atelier"
        d.mkdir(parents=True, exist_ok=True)
        quests = {i: {"branch": f"quest/{i.lower()}"} for i in ids}
        for i in stale:
            quests[i] = {"branch": f"quest/{i.lower()}"}
        (d / "manifest.json").write_text(
            json.dumps({"version": 1, "branch": "artist/atelier",
                        "quests": quests}),
            encoding="utf-8")

    def _seed_member(self, qid):
        qdir = self.root / ".court" / "quests"
        qdir.mkdir(parents=True, exist_ok=True)
        (qdir / f"{qid}.md").write_text(f"id: {qid}\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", f"charter {qid}")
        branch = f"quest/{qid.lower()}"
        self._git("checkout", "-q", "-b", branch)
        (self.root / f"{qid}.txt").write_text("work\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", f"work {qid}")
        self._git("checkout", "-q", "castle")

    def _add_atelier_worktree(self):
        subprocess.run(["git", "-C", str(self.root), "worktree", "add", "-q",
                        str(self.atelier_wt), "-b", "artist/atelier"],
                       check=True, capture_output=True)

    def _merge_into_atelier(self, qid):
        self._git("merge", "--no-edit", "-m",
                  f"court: merge {qid} into combined artist studio atelier",
                  f"quest/{qid.lower()}", cwd=self.atelier_wt)

    def test_is_atelier_branch(self):
        self.assertTrue(ui_server._is_atelier_branch("artist/atelier"))
        self.assertFalse(ui_server._is_atelier_branch("artist/q617-q627-ui-studio"))
        self.assertFalse(ui_server._is_atelier_branch(""))

    def test_resolves_members_from_the_repo_manifest(self):
        ids = ["Q1358-Common-WarehouseScannerUPCScanRate",
               "Q1403-Marketing-Trigger-Condition-Null-Operator-Inversion"]
        for qid in ids:
            self._seed_member(qid)
        self._add_atelier_worktree()
        for qid in ids:
            self._merge_into_atelier(qid)
        # Q1350 is a stale manifest entry (the rebuilt atelier lost its merge).
        self._manifest(ids, stale=["Q1350-Core-Web-Request-Ceiling-Successor"])
        # Read from the MAIN checkout's manifest, though we query the worktree.
        self.assertEqual(ui_server._studio_quest_ids(str(self.atelier_wt)), ids)
        ok, err = ui_server._check_studio_signoff_ready(str(self.atelier_wt))
        self.assertTrue(ok, err)

    def test_member_without_a_live_merge_is_not_offered(self):
        ids = ["Q1358-Common-WarehouseScannerUPCScanRate",
               "Q1362-Tasks-WorkerLightTier"]
        for qid in ids:
            self._seed_member(qid)
        self._add_atelier_worktree()
        self._merge_into_atelier(ids[0])
        self._manifest(ids)
        self.assertEqual(ui_server._studio_quest_ids(str(self.atelier_wt)), [ids[0]])

    def test_atelier_without_a_manifest_resolves_empty(self):
        self._add_atelier_worktree()
        self.assertEqual(ui_server._studio_quest_ids(str(self.atelier_wt)), [])


class TestSignoffCommand(unittest.TestCase):
    def test_patronize_allows_an_empty_revision(self):
        argv = ui_server._signoff_command(
            "patronize", "Q1358-Common-WarehouseScannerUPCScanRate", "M'Lord", "note")
        self.assertEqual(argv[-1], "--allow-empty")

    def test_plain_ui_signoff_does_not(self):
        argv = ui_server._signoff_command("ui-signoff", "Q1", "M'Lord", "note")
        self.assertNotIn("--allow-empty", argv)


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