"""kilo.db prune tool — zero-dep, chunked, verify-everything.

The event bus table grows unboundedly (67.7 GB of a 111.6 GB file on
2026-09-28); old sessions' rows are small but should still be aged out.
Doctrine:
  - dry-run by default; --apply required for mutations
  - keepers exported to a sidecar db BEFORE any delete
  - chunked DELETEs + wal_checkpoint(TRUNCATE) between chunks (bounded WAL on
    a nearly-full disk)
  - explicit child-first deletes (FK enforcement is off by default in SQLite)
  - in-place VACUUM as a separate step with a disk-headroom preflight
  - never touches account/project/credential/auth tables

Usage:
  python3 -m court.db_prune report
  python3 -m court.db_prune keep-backup [--days 14]
  python3 -m court.db_prune prune --days 14 --keep-events 1200000 [--apply]
  python3 -m court.db_prune vacuum [--apply]
  python3 -m court.db_prune verify
"""

import argparse
import os
import shutil
import sqlite3
import sys
import time

KILO_DB = os.path.expanduser("~/.local/share/kilo/kilo.db")
KILO_DIR = os.path.dirname(KILO_DB)
GB = 1024 ** 3

# session-scoped tables: (table, session column, chunk via old_ids temp table)
CHILD_TABLES = [
    ("part", "session_id"),
    ("message", "session_id"),
    ("todo", "session_id"),
    ("session_input", "session_id"),
    ("session_context_epoch", "session_id"),
    ("session_message", "session_id"),
    ("session_share", "session_id"),
    ("kilo_board_message", "board_root_session_id"),
    ("kilo_board", "root_session_id"),
]
EVENT_CHUNK = 400_000
SESSION_CHUNK = 200


def hr(n):
    return f"{n / GB:.2f} GB"


def disk_free():
    return shutil.disk_usage(KILO_DIR).free


def connect(apply_mode):
    db = sqlite3.connect(KILO_DB, timeout=60)
    db.execute("PRAGMA busy_timeout=60000")
    db.execute("PRAGMA foreign_keys=OFF")  # explicit child-first deletes
    if apply_mode:
        db.isolation_level = None  # autocommit; explicit BEGIN per chunk
    return db


def page_stats(db):
    ps = db.execute("PRAGMA page_size").fetchone()[0]
    pc = db.execute("PRAGMA page_count").fetchone()[0]
    fl = db.execute("PRAGMA freelist_count").fetchone()[0]
    return {"page_size": ps, "pages": pc, "freelist": fl,
            "size": ps * pc, "free": ps * fl, "live": ps * (pc - fl)}


def counts(db):
    out = {}
    for t in ("session", "message", "part", "event", "todo",
              "session_input", "session_context_epoch", "session_message",
              "session_share", "kilo_board", "kilo_board_message"):
        out[t] = db.execute(f"select count(*) from {t}").fetchone()[0]
    return out


def cmd_report(_):
    db = sqlite3.connect(f"file:{KILO_DB}?mode=ro", uri=True, timeout=30)
    st = page_stats(db)
    print(f"file: {KILO_DB}")
    print(f"size: {hr(st['size'])}  live: {hr(st['live'])}  "
          f"freelist(reclaimable): {hr(st['free'])}")
    print(f"disk free: {hr(disk_free())}")
    cutoff = int((time.time() - 14 * 86400) * 1000)
    n_old = db.execute(
        "select count(*) from session where time_updated < ?",
        (cutoff,)).fetchone()[0]
    n_all = db.execute("select count(*) from session").fetchone()[0]
    mx = db.execute("select max(rowid) from event").fetchone()[0] or 0
    print(f"sessions: {n_all} total, {n_all - n_old} newer than 14d, "
          f"{n_old} older (delete targets)")
    print(f"events: max rowid {mx}; a --keep-events 1200000 cutoff deletes "
          f"rowid <= {max(0, mx - 1_200_000)}")
    db.close()


def cmd_keep_backup(args):
    days = args.days
    cutoff = int((time.time() - days * 86400) * 1000)
    out = os.path.join(KILO_DIR, f"kilo_keep_{time.strftime('%Y%m%d')}.db")
    if os.path.exists(out):
        print(f"refusing: {out} exists (move it aside first)")
        return 1
    need = 6 * GB
    if disk_free() < need:
        print(f"refusing: need ~{hr(need)} free for the backup, "
              f"have {hr(disk_free())}")
        return 1
    db = sqlite3.connect(f"file:{KILO_DB}?mode=ro", uri=True, timeout=60)
    db.execute(f"ATTACH DATABASE '{out}' AS keep")
    print(f"snapshotting sessions updated since {cutoff} "
          f"({time.strftime('%Y-%m-%d', time.localtime(cutoff / 1000))})...")
    db.execute("CREATE TABLE keep.session AS "
               "SELECT * FROM main.session WHERE time_updated >= ?",
               (cutoff,))
    for t in ("message", "part", "todo", "session_input"):
        print(f"  {t}...", flush=True)
        db.execute(f"CREATE TABLE keep.{t} AS SELECT m.* FROM main.{t} m "
                   f"WHERE m.session_id IN "
                   f"(SELECT id FROM keep.session)")
    db.commit()
    kept = db.execute("select count(*) from keep.session").fetchone()[0]
    db.close()
    print(f"backup written: {out} ({hr(os.path.getsize(out))}, "
          f"{kept} sessions kept)")
    return 0


def cmd_prune(args):
    if not args.apply:
        print("DRY RUN — pass --apply to execute. See `report` for targets.")
        return cmd_report(args)
    if disk_free() < 12 * GB:
        print(f"refusing: need >=12 GB free headroom, have {hr(disk_free())}")
        return 1
    cutoff = int((time.time() - args.days * 86400) * 1000)
    db = connect(apply_mode=True)
    before = counts(db)
    print(f"before: {before}")
    db.execute("BEGIN")
    db.execute("CREATE TEMP TABLE old_ids AS "
               "SELECT id FROM session WHERE time_updated < ?"
               " AND id NOT IN (SELECT DISTINCT session_id FROM session_share)",
               (cutoff,))
    n_old = db.execute("select count(*) from old_ids").fetchone()[0]
    print(f"pruning {n_old} sessions older than {args.days}d "
          f"({time.strftime('%Y-%m-%d', time.localtime(cutoff / 1000))})")
    db.execute("CREATE INDEX temp.old_ids_pk ON old_ids(id)")
    db.commit()

    for table, col in CHILD_TABLES:
        total = 0
        while True:
            db.execute("BEGIN")
            chunk = [r[0] for r in db.execute(
                "SELECT id FROM old_ids LIMIT ? OFFSET ?",
                (SESSION_CHUNK, total)).fetchall()]
            if not chunk:
                db.execute("COMMIT")
                break
            q = ",".join("?" * len(chunk))
            cur = db.execute(
                f"DELETE FROM {table} WHERE {col} IN ({q})", chunk)
            total += len(chunk)
            db.execute("COMMIT")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            print(f"  {table}: {total}/{n_old} id-chunks, "
                  f"{cur.rowcount} rows last chunk", flush=True)
        print(f"{table}: done ({total} session ids scanned)")

    # events: delete oldest by rowid window (insertion-ordered), chunked
    mx = db.execute("select max(rowid) from event").fetchone()[0] or 0
    cutoff_rowid = max(0, mx - args.keep_events)
    deleted = 0
    lo = 1
    while lo <= cutoff_rowid:
        hi = min(lo + EVENT_CHUNK - 1, cutoff_rowid)
        db.execute("BEGIN")
        cur = db.execute("DELETE FROM event WHERE rowid >= ? AND rowid <= ?",
                         (lo, hi))
        n = cur.rowcount
        db.execute("COMMIT")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        deleted += n
        print(f"  events: rowid {lo}-{hi} deleted {n} "
              f"(total {deleted}, disk free {hr(disk_free())})", flush=True)
        lo = hi + 1
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # filesystem: per-session diff jsons for pruned sessions
    removed = 0
    diff_dir = os.path.join(KILO_DIR, "storage", "session_diff")
    old = [r[0] for r in db.execute("select id from old_ids")]
    db.close()
    if os.path.isdir(diff_dir):
        for sid in old:
            p = os.path.join(diff_dir, sid + ".json")
            if os.path.exists(p):
                os.remove(p)
                removed += 1
    print(f"session_diff files removed: {removed}")
    db2 = connect(apply_mode=True)
    after = counts(db2)
    st = page_stats(db2)
    print(f"after: {after}")
    print(f"file now: {hr(st['size'])} (freelist {hr(st['free'])}) — "
          f"run `vacuum --apply` to reclaim it")
    db2.close()
    return 0


def cmd_vacuum(args):
    if not args.apply:
        st = page_stats(sqlite3.connect(f"file:{KILO_DB}?mode=ro", uri=True))
        est = st["live"] + 2 * GB
        print(f"DRY RUN — VACUUM rewrites {hr(st['live'])} of live data; "
              f"needs ~{hr(est)} free, have {hr(disk_free())}.")
        return 0
    need = page_stats(sqlite3.connect(
        f"file:{KILO_DB}?mode=ro", uri=True))["live"] + 2 * GB
    if disk_free() < need:
        print(f"refusing: need ~{hr(need)} free, have {hr(disk_free())}")
        return 1
    db = connect(apply_mode=True)
    t0 = time.time()
    print("VACUUM running (exclusive lock at start; minutes)...", flush=True)
    db.execute("VACUUM")
    st = page_stats(db)
    print(f"done in {time.time() - t0:.0f}s — file now {hr(st['size'])}, "
          f"freelist {hr(st['free'])}")
    db.close()
    return 0


def cmd_verify(_):
    db = sqlite3.connect(f"file:{KILO_DB}?mode=ro", uri=True, timeout=30)
    ok = db.execute("PRAGMA integrity_check").fetchone()[0]
    st = page_stats(db)
    c = counts(db)
    print(f"integrity_check: {ok}")
    print(f"size: {hr(st['size'])} live: {hr(st['live'])} "
          f"freelist: {hr(st['free'])}")
    print(f"counts: {c}")
    db.close()
    return 0 if ok == "ok" else 1


def main():
    ap = argparse.ArgumentParser(prog="court.db_prune")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("report")
    kb = sub.add_parser("keep-backup")
    kb.add_argument("--days", type=int, default=14)
    pr = sub.add_parser("prune")
    pr.add_argument("--days", type=int, default=14)
    pr.add_argument("--keep-events", type=int, default=1_200_000)
    pr.add_argument("--apply", action="store_true")
    vc = sub.add_parser("vacuum")
    vc.add_argument("--apply", action="store_true")
    sub.add_parser("verify")
    args = ap.parse_args()
    return {"report": cmd_report, "keep-backup": cmd_keep_backup,
            "prune": cmd_prune, "vacuum": cmd_vacuum,
            "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
