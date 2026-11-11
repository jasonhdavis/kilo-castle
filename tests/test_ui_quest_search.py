"""Tests for the UI quest-charter search helper (court/ui_server._quest_search).

Search is over quest id, title, status, app, AND the charter body text, with a
snippet when the hit is body-only.
"""
from pathlib import Path

from court import ui_server


def _mk_quest(root: Path, qid: str, title: str, status: str, app: str, body: str) -> None:
    d = root / ".court" / "quests"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{qid}.md").write_text(
        f"---\nid: {qid}\ntitle: {title}\nstatus: {status}\napp: {app}\n---\n\n{body}\n",
        encoding="utf-8")


def test_search_by_id_ranks_first(tmp_path):
    _mk_quest(tmp_path, "Q1403-Marketing-Trigger", "Trigger null inversion", "GATE", "Marketing", "body")
    _mk_quest(tmp_path, "Q1404-Other", "Unrelated", "OPEN", "Common", "mentions Q1403 in passing")
    res = ui_server._quest_search(str(tmp_path), "q1403")
    assert res and res[0]["id"] == "Q1403-Marketing-Trigger"


def test_search_by_title(tmp_path):
    _mk_quest(tmp_path, "Q1-A", "Warehouse scanner UPC scan rate", "GATE", "Common", "x")
    _mk_quest(tmp_path, "Q2-B", "Something else entirely", "OPEN", "Core", "x")
    res = ui_server._quest_search(str(tmp_path), "scanner upc")
    assert [m["id"] for m in res] == ["Q1-A"]


def test_search_by_body_returns_a_snippet(tmp_path):
    _mk_quest(tmp_path, "Q2-B", "Nothing obvious", "OPEN", "Core",
              "The zanzibar token must rotate hourly.\nOther lines here.")
    res = ui_server._quest_search(str(tmp_path), "zanzibar")
    assert res and res[0]["id"] == "Q2-B"
    assert "zanzibar" in res[0]["snippet"].lower()


def test_multi_term_query_is_an_and(tmp_path):
    _mk_quest(tmp_path, "Q3-C", "Alpha beta", "OPEN", "Core", "x")
    assert ui_server._quest_search(str(tmp_path), "alpha gamma") == []
    assert [m["id"] for m in ui_server._quest_search(str(tmp_path), "alpha beta")] == ["Q3-C"]


def test_empty_and_no_match_return_nothing(tmp_path):
    _mk_quest(tmp_path, "Q4-D", "Alpha", "OPEN", "Core", "x")
    assert ui_server._quest_search(str(tmp_path), "") == []
    assert ui_server._quest_search(str(tmp_path), "zzzznotfound") == []
