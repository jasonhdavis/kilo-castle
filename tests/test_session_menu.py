"""Session-menu status glyphs (court.ui_server): the per-session turn tail that
/api/session/starts ships alongside the first-user-text snippet. Pure reads of
a throwaway sqlite file — never the real kilo.db."""

import json
import sqlite3

import pytest

from court import ui_server as u


@pytest.fixture()
def kilo_db(tmp_path, monkeypatch):
    path = tmp_path / "kilo.db"
    db = sqlite3.connect(path)
    db.executescript(
        "create table message(id text, session_id text, time_created integer,"
        " data text);"
        "create table part(id text, message_id text, data text);")
    db.commit()
    db.close()
    monkeypatch.setattr(u, "KILO_DB", str(path))
    return path


def _msg(path, sid, mid, when, role, finish=None):
    db = sqlite3.connect(path)
    db.execute("insert into message values (?,?,?,?)",
               (mid, sid, when, json.dumps({"role": role, "finish": finish})))
    db.commit()
    db.close()


def _text(path, mid, text):
    db = sqlite3.connect(path)
    db.execute("insert into part values (?,?,?)",
               (mid + "-p", mid, json.dumps({"type": "text", "text": text})))
    db.commit()
    db.close()


def _last(path, sid):
    db = sqlite3.connect(path)
    row = db.execute(
        "select json_extract(data,'$.role'), json_extract(data,'$.finish')"
        " from message where session_id=? order by time_created desc limit 1",
        (sid,)).fetchone()
    db.close()
    return row


def test_completed_turn_reports_start_and_finish(kilo_db):
    _msg(kilo_db, "ses_done", "m1", 1, "user")
    _text(kilo_db, "m1", "implement the thing")
    _msg(kilo_db, "ses_done", "m2", 2, "assistant", "stop")
    got = u._session_starts(["ses_done"])["ses_done"]
    assert got["start"] == "implement the thing"
    assert got["role"] == "assistant"
    assert got["finish"] == "stop"


def test_unanswered_prompt_reports_user_tail(kilo_db):
    _msg(kilo_db, "ses_queued", "m1", 1, "user")
    _msg(kilo_db, "ses_queued", "m2", 2, "assistant", "tool-calls")
    _msg(kilo_db, "ses_queued", "m3", 3, "user")
    got = u._session_starts(["ses_queued"])["ses_queued"]
    assert got["role"] == "user"
    assert got["finish"] is None


def test_cut_mid_write_reports_null_finish(kilo_db):
    _msg(kilo_db, "ses_cut", "m1", 1, "assistant", None)
    got = u._session_starts(["ses_cut"])["ses_cut"]
    assert got["role"] == "assistant"
    assert got["finish"] is None
    assert "start" not in got


def test_missing_message_rows_yield_nothing(kilo_db):
    assert u._session_starts(["ses_empty"]) == {}


def test_page_ships_the_menu_glyph():
    assert "function turnGlyph" in u.PAGE
    assert "${turnGlyph(st,liveFor(s))}" in u.PAGE
