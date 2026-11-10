"""Charter requests: the Pleasance lane's requests-in / charters-out contract.

A request carries intent and no Quest id, so the voice agent can never
introduce a duplicate number; a Steward allocates it from the board.
"""
from __future__ import annotations

import json

import pytest

from court import requests


def _packet(**kw):
    base = {
        "id": "req-2026-11-09-faire-relay-audit",
        "title": "Faire relay-email audit",
        "ask": "Audit Faire relay e-mail eligibility and pillory mismatches.",
        "requested_by": "voice-agent",
    }
    base.update(kw)
    return base


def test_a_valid_packet_round_trips():
    packet = requests.parse_request(json.dumps(_packet()))
    assert packet["id"] == "req-2026-11-09-faire-relay-audit"
    assert packet["chartered_quest"] == ""
    assert "Request: req-2026-11-09-faire-relay-audit" in requests.render_brief(packet)


@pytest.mark.parametrize("patch,needle", [
    ({"id": "Q1370"}, "id must be"),
    ({"title": "x"}, "title must be"),
    ({"ask": "too short"}, "ask must be"),
    ({"constraints": "not-a-list"}, "constraints must be"),
    ({"why": ["nope"]}, "why must be a string"),
])
def test_invalid_packets_are_refused(patch, needle):
    ok, why = requests.validate_request(json.dumps(_packet(**patch)))
    assert not ok and needle in why


def test_a_request_carries_no_quest_number():
    packet = requests.parse_request(json.dumps(_packet()))
    assert "quest" not in packet["id"]
    assert packet["chartered_quest"] == ""


def test_write_list_and_close(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".court").mkdir(parents=True)
    path = requests.write_request(repo, title="Ship the thing", ask="Do the thing end to end.",
                                  why="because", requested_by="voice-agent")
    assert path.is_file()
    open_requests = requests.load_open(repo)
    assert [r["id"] for r in open_requests] == [path.stem]

    requests.mark_chartered(repo, path.stem, "Q1400-Ship-The-Thing")
    assert requests.load_open(repo) == []
    stored = json.loads(path.read_text())
    assert stored["chartered_quest"] == "Q1400-Ship-The-Thing"


def test_unreadable_packets_are_skipped_not_fatal(tmp_path):
    repo = tmp_path / "repo"
    d = repo / ".court" / "requests"
    d.mkdir(parents=True)
    (d / "broken.json").write_text("{not json")
    (d / "good.json").write_text(json.dumps(_packet()))
    assert [r["id"] for r in requests.load_open(repo)] == [_packet()["id"]]
