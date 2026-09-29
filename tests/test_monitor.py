"""Agent & Process Monitor (court.ui_server /monitor): pure helpers only —
ps parsing, schedule-file parsing, session status boundaries, orphan and
resurrection detection, and the snapshot builder. Nothing here touches the
real kilo.db, the real schedule stores, or the live process table."""

import json
import os
import time

import pytest

from court import ui_server as u


PS_FIXTURE = """\
  40644 40565   04:39:43   2:14.31   1.3 /opt/homebrew/Python -u -m court.cli ui --port 8300
  45491     1 01-15:49:57   0:16.23   0.0 /venv/bin/python manage.py runserver 127.0.0.1:8251 --noreload
  63699 40644       02:11   0:17.26   1.7 /Users/x/.kilo/bin/kilo run --dir /work --session ses_abc
  27562     1    16:10:40   0:15.20   0.0 /ext/kilo __background-process-runner 224205f3 x
  27574 27562    16:10:39   0:00.01   0.0 /bin/zsh -l -c eval "run it"
  40565 27562   04:39:44   0:00.01   0.0 /bin/zsh -l -c eval "python -m court.cli ui"
  66435 27574       00:50   0:02.00   2.3 /venv/bin/python -m pytest apps -q
  66498 66435       00:48   0:31.86  84.1 /venv/bin/python -u -c import sys
   57407     1    15:56:43   0:00.88   0.0 python manage.py runserver 0.0.0.0:8256
  63944 63702 01-03:00:25   0:23.85   0.0 /Code Helper (Plugin) server.bundle.js --node-ipc
"""


# --- ps output parsing ------------------------------------------------------

def test_parse_ps_output_fields():
    rows = u.parse_ps_output(PS_FIXTURE)
    assert len(rows) == 10
    first = rows[0]
    assert first["pid"] == 40644
    assert first["ppid"] == 40565
    assert first["etime"] == "04:39:43"
    assert first["cpu_time"] == "2:14.31"
    assert first["cpu_pct"] == 1.3
    assert first["command"].endswith("--port 8300")


def test_parse_ps_output_command_keeps_inner_spaces():
    rows = u.parse_ps_output(PS_FIXTURE)
    assert rows[2]["command"] == \
        "/Users/x/.kilo/bin/kilo run --dir /work --session ses_abc"
    assert rows[2]["cpu_pct"] == 1.7


def test_parse_ps_output_skips_garbage_lines():
    rows = u.parse_ps_output("junk line\n\n  not-a-pid x y z w\n" + PS_FIXTURE)
    assert len(rows) == 10
    assert u.parse_ps_output("") == []
    assert u.parse_ps_output(None) == []


@pytest.mark.parametrize("text,seconds", [
    ("05:12", 312.0),
    ("0:07.34", 7.34),
    ("1:02.44", 62.44),
    ("47:21.59", 2841.59),
    ("16:10:40", 58240.0),
    ("01-15:49:57", 143397.0),
    ("", 0.0),
    ("junk", 0.0),
])
def test_parse_ps_duration(text, seconds):
    assert u.parse_ps_duration(text) == seconds


# --- session status classification (chip boundaries) ------------------------

def test_classify_session_boundaries():
    assert u.classify_session(0) == "active"
    assert u.classify_session(4 * 60 * 1000 + 59 * 1000) == "active"  # 4m59s
    assert u.classify_session(5 * 60 * 1000) == "idle"               # 5m sharp
    assert u.classify_session(59 * 60 * 1000) == "idle"              # 59m
    assert u.classify_session(59 * 60 * 1000 + 59 * 1000) == "idle"  # 59m59s
    assert u.classify_session(60 * 60 * 1000) == "stale"             # 60m sharp


def test_classify_session_missing_heartbeat_is_stale():
    assert u.classify_session(None) == "stale"


# --- schedule file parsing ---------------------------------------------------

def _write_sched(dirs_root, kind, sid, name, payload):
    d = dirs_root / kind / sid
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(json.dumps(payload) if isinstance(payload, dict) else payload)
    return str(p)


def test_load_schedule_files_parses_cron_and_wakeup(tmp_path):
    cron = _write_sched(tmp_path, "cron", "ses_owner1", "sched.json", {
        "sessionID": "ses_owner1", "schedule": "*/20 * * * *",
        "recurring": True, "dueAt": None, "expiresAt": None,
        "prompt": "check the deploy"})
    wake = _write_sched(tmp_path, "wakeup", "ses_owner2", "w.json", {
        "sessionID": "ses_owner2", "recurring": False,
        "dueAt": 1790703438000, "expiresAt": None,
        "prompt": "one shot reminder"})
    rows = u.load_schedule_files([str(tmp_path / "cron"),
                                  str(tmp_path / "wakeup")])
    assert len(rows) == 2
    by_path = {r["path"]: r for r in rows}
    c = by_path[cron]
    assert c["kind"] == "cron"
    assert c["session_id"] == "ses_owner1"
    assert c["schedule"] == "*/20 * * * *"
    assert c["recurring"] is True
    assert c["prompt"] == "check the deploy"
    assert c["broken"] is False
    w = by_path[wake]
    assert w["kind"] == "wakeup"
    assert w["recurring"] is False
    assert w["due_at"] == 1790703438000
    assert w["schedule"] is None


def test_load_schedule_files_broken_json_surfaces(tmp_path):
    _write_sched(tmp_path, "cron", "ses_x", "bad.json", "{not json")
    rows = u.load_schedule_files([str(tmp_path / "cron")])
    assert len(rows) == 1
    assert rows[0]["broken"] is True
    assert rows[0]["session_id"] == ""


def test_load_schedule_files_missing_dir_is_empty(tmp_path):
    assert u.load_schedule_files([str(tmp_path / "nope")]) == []


def test_load_schedule_files_ignores_non_json(tmp_path):
    _write_sched(tmp_path, "cron", "ses_x", "notes.txt", "hello")
    assert u.load_schedule_files([str(tmp_path / "cron")]) == []


def test_sched_num_accepts_epoch_ms_and_iso():
    assert u._sched_num(1790703438000) == 1790703438000
    assert u._sched_num(None) is None
    assert u._sched_num("2026-09-29T17:37:18Z") == 1790703438000
    assert u._sched_num("not-a-date") is None


# --- orphan detection --------------------------------------------------------

def _sched(sid, **kw):
    row = {"kind": "cron", "path": "/x/sched.json", "session_id": sid,
           "schedule": None, "recurring": False, "due_at": None,
           "expires_at": None, "prompt": "", "broken": False, "mtime": 0}
    row.update(kw)
    return row


def test_detect_orphans_flags_missing_owners():
    schedules = [
        _sched("ses_live1"),
        _sched("ses_gone"),
        _sched(""),  # broken/unparseable: no owner -> not orphan-flagged
    ]
    orphans = u.detect_orphans(schedules, {"ses_live1", "ses_live2"})
    assert [s["session_id"] for s in orphans] == ["ses_gone"]
    assert schedules[0]["orphaned"] is False
    assert schedules[1]["orphaned"] is True
    assert schedules[2]["orphaned"] is False


def test_detect_orphans_empty_session_table_orphans_everything():
    schedules = [_sched("ses_a"), _sched("ses_b")]
    orphans = u.detect_orphans(schedules, set())
    assert len(orphans) == 2


def test_mark_resurrections_matches_logged_path():
    log = {"/store/cron/ses_a/1.json": {"ts": time.time()}}
    schedules = [_sched("ses_a", path="/store/cron/ses_a/1.json"),
                 _sched("ses_b", path="/store/cron/ses_b/2.json")]
    hit = u.mark_resurrections(schedules, log)
    assert [s["path"] for s in hit] == ["/store/cron/ses_a/1.json"]
    assert schedules[0]["resurrected"] is True
    assert schedules[1]["resurrected"] is False


# --- process scoping and decoration -----------------------------------------

def test_select_monitor_processes_scope():
    procs = u.parse_ps_output(PS_FIXTURE)
    scoped = u.select_monitor_processes(procs)
    pids = {p["pid"] for p in scoped}
    # kilo run, kilo background-runner, python court ui, python runserver,
    # python -c, and the zsh child of the runner are all in scope
    assert {40644, 40565, 45491, 63699, 27562, 27574, 66435, 66498, 57407} <= pids
    # code helper with no kilo/python ancestry stays out
    assert 63944 not in pids


def test_select_monitor_processes_shells_under_kilo_are_in_scope():
    text = (
        "  100     1   00:00:01   0:00.10   0.0 /bin/kilo serve --port 0\n"
        "  200   100   00:00:01   0:00.10   0.0 /bin/zsh -c do work\n"
        "  300   200   00:00:01   0:00.10   0.0 tail -f log\n")
    pids = {p["pid"] for p in u.select_monitor_processes(
        u.parse_ps_output(text))}
    assert pids == {100, 200, 300}


def test_select_monitor_processes_kilo_substring_in_paths_is_not_scope():
    # a process that merely mentions a kilo-containing path is out of scope
    text = ("  500     1   00:00:01   0:00.10   0.0 "
            "/usr/bin/git -C /repo/kilo-castle status\n"
            "  501     1   00:00:01   0:00.10   0.0 "
            "Chrome --user-data-dir=/x/kilo-castle/profile\n")
    assert u.select_monitor_processes(u.parse_ps_output(text)) == []


def test_decorate_processes_flags():
    procs = u.parse_ps_output(PS_FIXTURE)
    # 57407 (ppid 1) and 45491 (ppid 1) orphans; 45491 is >24h; 66498 is hot
    # with a live parent; 63944's parent (63702) is not in the table -> orphan
    dec = u.decorate_processes(procs, now=time.time(),
                               protected=[40644])
    by_pid = {p["pid"]: p for p in dec}
    assert by_pid[45491]["orphan"] and by_pid[45491]["old"]
    assert by_pid[57407]["orphan"] and not by_pid[57407]["old"]
    assert by_pid[66498]["hot"] and not by_pid[66498]["orphan"]
    assert by_pid[63944]["orphan"]  # dead parent (ppid missing)
    assert not by_pid[40644]["orphan"]  # parent 40565 is in the table
    assert by_pid[40644]["self"] is True
    assert by_pid[45491]["self"] is False


def test_protected_chain_walks_ancestors():
    # 30 -> 20 -> 10 -> 1: chain is [30, 20, 10]
    chain = u._protected_chain(lookup=lambda pid: {30: 20, 20: 10, 10: 1}.get(pid),
                               start=30)
    assert chain == [30, 20, 10]


# --- cron next-fire ----------------------------------------------------------

def test_next_cron_fire_every_20_minutes():
    now = 1790703438.0  # 2026-09-29 17:37:18 UTC
    nxt = u.next_cron_fire("*/20 * * * *", now)
    assert nxt == 1790703600.0  # 17:40:00 UTC


def test_next_cron_fire_weekday_morning():
    now = 1790703438.0  # Tuesday 17:37 UTC
    nxt = u.next_cron_fire("30 9 * * 1-5", now)
    assert time.strftime("%Y-%m-%d %H:%M", time.gmtime(nxt)) == \
        "2026-09-30 09:30"


def test_next_cron_fire_sunday_as_seven():
    now = 1790534000.0  # Sunday 2026-09-27 17:13 UTC
    nxt = u.next_cron_fire("0 12 * * 7", now)
    assert time.strftime("%a %H:%M", time.gmtime(nxt)) == "Sun 12:00"


def test_next_cron_fire_never_in_past_and_bad_expr():
    now = 1790703438.0
    assert u.next_cron_fire("* * * *", now) is None
    nxt = u.next_cron_fire("0 0 1 1 *", now)
    assert time.strftime("%Y-%m-%d", time.gmtime(nxt)) == "2027-01-01"


# --- snapshot builder --------------------------------------------------------

def _sess(sid, heartbeat, cost=0.0, directory="/repo/alpha", agent="serf",
          model="z-ai/glm-5.3-flash"):
    return {"id": sid, "directory": directory, "agent": agent, "model": model,
            "time_created": heartbeat - 60000, "time_updated": heartbeat,
            "cost": cost, "parent_id": None, "heartbeat": heartbeat}


def _proc(pid, ppid, etime_s=60.0, cpu_pct=0.0, command=""):
    return {"pid": pid, "ppid": ppid, "etime": "00:01:00", "cpu_time": "0:00.5",
            "etime_s": etime_s, "cpu_time_s": 0.5, "cpu_pct": cpu_pct,
            "command": command}


NOW_MS = 1_791_000_000_000
TODAY_START = NOW_MS - 3_600_000  # one hour before "now"


def test_build_monitor_snapshot_counts_and_status():
    sessions = [
        _sess("ses_a", NOW_MS - 1 * 60 * 1000, cost=1.5),   # active
        _sess("ses_b", NOW_MS - 30 * 60 * 1000, cost=0.5),  # idle
        _sess("ses_c", NOW_MS - 90 * 60 * 1000, cost=2.0),  # stale
        _sess("ses_d", TODAY_START - 1, cost=99.0),         # stale, yesterday
    ]
    schedules = [_sched("ses_gone", recurring=True, schedule="*/20 * * * *"),
                 _sched("ses_a", recurring=False, due_at=NOW_MS - 1000)]
    procs = [
        _proc(10, 1, command="kilo serve"),                 # orphan
        _proc(11, 10, etime_s=30 * 3600, command="python runserver"),  # old
        _proc(12, 11, cpu_pct=80.0,
              command="python -m court.cli ui"),            # hot, not orphan
    ]
    snap = u.build_monitor_snapshot(
        sessions, schedules, procs, now_ms=NOW_MS,
        today_start_ms=TODAY_START, delete_log={})
    assert snap["summary"]["orphaned_schedules"] == 1
    assert snap["summary"]["stale_sessions"] == 2  # ses_c + ses_d
    assert snap["summary"]["zombie_processes"] == 1
    # cost today: sessions with heartbeat at/after midnight window only
    assert snap["summary"]["cost_today"] == pytest.approx(1.5 + 0.5)
    # orphans sort first
    assert snap["schedules"][0]["orphaned"] is True
    assert snap["schedules"][1]["orphaned"] is False
    # session statuses
    st = {s["id"]: s["status"] for s in snap["sessions"]}
    assert st == {"ses_a": "active", "ses_b": "idle", "ses_c": "stale",
                  "ses_d": "stale"}
    assert snap["session_total"] == 4


def test_build_monitor_snapshot_links_live_pids():
    sessions = [_sess("ses_abc", NOW_MS - 1000)]
    procs = [
        _proc(10, 1, command="kilo run --session ses_abc"),
        _proc(11, 10, command="zsh -c helper"),
    ]
    snap = u.build_monitor_snapshot(sessions, [], procs, now_ms=NOW_MS,
                                    delete_log={})
    assert snap["sessions"][0]["pids"] == [10]


def test_build_monitor_snapshot_caps_at_50_most_recent():
    sessions = [_sess("ses_%03d" % i, NOW_MS - i * 1000, cost=0.01)
                for i in range(55)]
    snap = u.build_monitor_snapshot(sessions, [], [], now_ms=NOW_MS,
                                    delete_log={})
    assert len(snap["sessions"]) == 50
    assert snap["session_total"] == 55
    # most-recent-first ordering
    assert snap["sessions"][0]["id"] == "ses_000"


def test_build_monitor_snapshot_past_due_and_resurrected():
    sessions = [_sess("ses_a", NOW_MS)]
    schedules = [
        _sched("ses_a", recurring=False, due_at=NOW_MS - 5000,
               path="/c/ses_a/1.json"),
        _sched("ses_a", recurring=True, schedule="*/5 * * * *",
               path="/c/ses_a/2.json"),
        _sched("ses_a", recurring=False, due_at=NOW_MS + 60000,
               path="/c/ses_a/3.json", expires_at=NOW_MS - 1),
    ]
    snap = u.build_monitor_snapshot(sessions, schedules, [], now_ms=NOW_MS,
                                    delete_log={"/c/ses_a/3.json":
                                                {"ts": NOW_MS / 1000}})
    by_path = {s["path"]: s for s in snap["schedules"]}
    assert by_path["/c/ses_a/1.json"]["past_due"] is True
    assert by_path["/c/ses_a/1.json"]["next_due"] == NOW_MS - 5000
    assert by_path["/c/ses_a/2.json"]["past_due"] is False
    assert by_path["/c/ses_a/2.json"]["next_due"] > NOW_MS
    assert by_path["/c/ses_a/3.json"]["expired"] is True
    assert by_path["/c/ses_a/3.json"]["resurrected"] is True
    assert snap["summary"]["resurrected_schedules"] == 1


def test_build_monitor_snapshot_owner_projection():
    sessions = [_sess("ses_a", NOW_MS, directory="/Users/x/Python/kilo-castle")]
    schedules = [_sched("ses_a", recurring=True, schedule="*/5 * * * *")]
    snap = u.build_monitor_snapshot(sessions, schedules, [], now_ms=NOW_MS,
                                    delete_log={})
    s = snap["schedules"][0]
    assert s["owner_project"] == "kilo-castle"
    assert s["owner_model"] == "z-ai/glm-5.3-flash"


def test_killer_script_shape():
    """The detached killer source TERM-waits-KILLs; keep it honest."""
    assert "signal.SIGTERM" in u._KILLER_SRC
    assert "signal.SIGKILL" in u._KILLER_SRC
    assert "time.sleep(0.2)" in u._KILLER_SRC
    for _ in range(15):
        pass
    assert u._KILLER_SRC.count("time.sleep(0.2)") == 1  # looped 15x -> ~3s
    assert "for _ in range(15):" in u._KILLER_SRC


def test_monitor_page_has_no_internal_jargon():
    page = u.MONITOR_PAGE
    for word in ("Tribute", "Serf", "Kingdom", "Castle", "Court", "Penance",
                 "Ballad", "Tally", "Pillory", "Cogship", "steward",
                 "master_of_coin", "gatekeeper"):
        assert word not in page, word