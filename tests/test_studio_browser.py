"""Unit tests for the shared studio browser's annotation relink and injector
liveness (court/browser.py) — the 2026-10-17 studio reliability hardening.

Covers: annotation-layer reset when a studio adopts a browser pinned to a
different worktree, injector respawn after death, and the injector surviving a
transient browser drop instead of exiting.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from court import browser


class TestRelinkAnnotator(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.old_wt = self.tmp / "old-worktree"
        self.new_wt = self.tmp / "new-worktree"
        self.old_wt.mkdir()
        self.new_wt.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_relink_kills_old_injector_and_respawns_on_new_worktree(self):
        state = {
            "pid": 1, "port": 9335,
            "annotator_pid": 111, "annotate_worktree": str(self.old_wt.resolve()),
        }
        with patch.object(browser, "_pid_alive", return_value=True), \
             patch.object(browser, "_terminate_pid") as mock_kill, \
             patch.object(browser, "_spawn_injector", return_value=222) as mock_spawn, \
             patch.object(browser, "_write_state") as mock_write:
            browser._relink_annotator(state, self.new_wt)

        mock_kill.assert_called_once_with(111)
        mock_spawn.assert_called_once()
        self.assertEqual(state["annotator_pid"], 222)
        self.assertEqual(state["annotate_worktree"], str(self.new_wt.resolve()))
        mock_write.assert_called_once()

    def test_relink_is_noop_when_same_worktree_and_injector_alive(self):
        state = {
            "pid": 1, "port": 9335,
            "annotator_pid": 111, "annotate_worktree": str(self.old_wt.resolve()),
        }
        with patch.object(browser, "_pid_alive", return_value=True), \
             patch.object(browser, "_terminate_pid") as mock_kill, \
             patch.object(browser, "_spawn_injector") as mock_spawn, \
             patch.object(browser, "_write_state") as mock_write:
            browser._relink_annotator(state, self.old_wt)

        mock_kill.assert_not_called()
        mock_spawn.assert_not_called()
        mock_write.assert_not_called()

    def test_relink_respawns_when_recorded_injector_is_dead(self):
        state = {
            "pid": 1, "port": 9335,
            "annotator_pid": 111, "annotate_worktree": str(self.old_wt.resolve()),
        }
        with patch.object(browser, "_pid_alive", return_value=False), \
             patch.object(browser, "_terminate_pid") as mock_kill, \
             patch.object(browser, "_spawn_injector", return_value=222) as mock_spawn, \
             patch.object(browser, "_write_state") as mock_write:
            browser._relink_annotator(state, self.old_wt)

        mock_kill.assert_not_called()
        mock_spawn.assert_called_once()
        self.assertEqual(state["annotator_pid"], 222)
        mock_write.assert_called_once()

    def test_relink_ignored_without_target_worktree(self):
        state = {"pid": 1, "port": 9335}
        with patch.object(browser, "_spawn_injector") as mock_spawn, \
             patch.object(browser, "_write_state") as mock_write:
            browser._relink_annotator(state, None)
        mock_spawn.assert_not_called()
        mock_write.assert_not_called()


class TestInjectorSurvivesTransientDrop(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.stop = self.tmp / "stop"
        self.wt = self.tmp / "wt"
        self.wt.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_injector_retries_when_cdp_is_down_then_exits_on_stop_file(self):
        """A browser that is momentarily unreachable must not end the loop."""
        calls = {"n": 0}

        def fake_cdp(port):
            calls["n"] += 1
            if calls["n"] >= 3:
                self.stop.write_text("stop", encoding="utf-8")
            return False

        with patch.multiple(
                browser,
                POLL_SECONDS=0.01,
                STOP_FILE_PATH=self.stop,
                _load_state=MagicMock(return_value={"pid": 4242, "port": 9335}),
                cdp_responsive=fake_cdp):
            browser._inject_worker(str(self.wt))

        # It polled past the first (and second) cdp failure before the stop file
        # appeared, rather than breaking out immediately.
        self.assertGreaterEqual(calls["n"], 3)


if __name__ == "__main__":
    unittest.main()
