"""Unit tests for court.db_prune — parent-row purge regression (candidate #5).

The 2026-10-05 incident: cmd_prune deleted child tables + events + diff files
but never the parent session rows, so every pass re-selected the same orphan
shells ("after: session 2481" unchanged across three passes) and kilo.db grew
back with dead parent rows.
"""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from court import db_prune


SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY,
    directory TEXT,
    time_updated INTEGER,
    title TEXT
);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT);
CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT);
CREATE TABLE todo (id TEXT PRIMARY KEY, session_id TEXT);
CREATE TABLE session_input (id TEXT PRIMARY KEY, session_id TEXT);
CREATE TABLE session_context_epoch (id TEXT PRIMARY KEY, session_id TEXT);
CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT);
CREATE TABLE session_share (id TEXT PRIMARY KEY, session_id TEXT);
CREATE TABLE kilo_board (id TEXT PRIMARY KEY, root_session_id TEXT);
CREATE TABLE kilo_board_message (id TEXT PRIMARY KEY, board_root_session_id TEXT);
CREATE TABLE event (id INTEGER PRIMARY KEY AUTOINCREMENT, t TEXT);
"""

NOW_MS = int(time.time() * 1000)
OLD = NOW_MS - 30 * 86400 * 1000
NEW = NOW_MS - 1 * 86400 * 1000


def _mkdb(path, n_old=3, n_new=2):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    rows = []
    for i in range(n_old):
        sid = f"old-{i}"
        rows.append((sid, "/tmp/x", OLD, f"old {i}"))
    for i in range(n_new):
        sid = f"new-{i}"
        rows.append((sid, "/tmp/x", NEW, f"new {i}"))
    db.executemany("INSERT INTO session VALUES (?,?,?,?)", rows)
    for sid, _, _, _ in rows:
        db.execute("INSERT INTO message VALUES (?,?)", (f"m-{sid}", sid))
        db.execute("INSERT INTO part VALUES (?,?)", (f"p-{sid}", sid))
    # one share-protected OLD session: must survive pruning entirely
    db.execute("INSERT INTO session VALUES ('old-shared', '/tmp/x', ?, 'shared')", (OLD,))
    db.execute("INSERT INTO session_share VALUES ('s1', 'old-shared')")
    db.execute("INSERT INTO message VALUES ('m-old-shared', 'old-shared')")
    # events: 10 rows, keep-events 5 -> oldest 5 deleted
    for i in range(10):
        db.execute("INSERT INTO event (t) VALUES (?)", (f"e{i}",))
    db.commit()
    db.close()


class TestDbPruneParentPurge(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.db_path = self.dir / "kilo.db"
        _mkdb(str(self.db_path))

    def tearDown(self):
        self._tmp.cleanup()

    def test_prune_deletes_parent_rows_one_pass(self):
        with patch.object(db_prune, "KILO_DB", str(self.db_path)), \
             patch.object(db_prune, "KILO_DIR", str(self.dir)):
            args = type("A", (), {})()
            args.apply = True
            args.days = 14
            args.keep_events = 5
            rc = db_prune.cmd_prune(args)
        self.assertEqual(rc, 0)
        db = sqlite3.connect(str(self.db_path))
        sids = sorted(r[0] for r in db.execute("select id from session"))
        self.assertEqual(sids, ["new-0", "new-1", "old-shared"])
        # children of pruned parents gone; shared-protected children intact
        self.assertEqual(db.execute("select count(*) from message where session_id like 'old-%' and session_id != 'old-shared'").fetchone()[0], 0)
        self.assertEqual(db.execute("select count(*) from part where session_id like 'old-%' and session_id != 'old-shared'").fetchone()[0], 0)
        self.assertEqual(db.execute("select count(*) from message where session_id='old-shared'").fetchone()[0], 1)
        # events: oldest 5 deleted, newest 5 kept
        self.assertEqual(db.execute("select count(*) from event").fetchone()[0], 5)
        db.close()

    def test_second_pass_is_noop(self):
        with patch.object(db_prune, "KILO_DB", str(self.db_path)), \
             patch.object(db_prune, "KILO_DIR", str(self.dir)):
            args = type("A", (), {})()
            args.apply = True
            args.days = 14
            args.keep_events = 5
            db_prune.cmd_prune(args)
            before = sqlite3.connect(str(self.db_path)).execute(
                "select count(*) from session").fetchone()[0]
            db_prune.cmd_prune(args)
            after = sqlite3.connect(str(self.db_path)).execute(
                "select count(*) from session").fetchone()[0]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()