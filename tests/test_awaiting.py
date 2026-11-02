"""Tests for the `court awaiting` three-dam digest (pure grouping)."""
import unittest

from court.cli import _awaiting_buckets
from court.models import Quest


def _quest(qid, status, kind="quest"):
    return Quest(id=qid, title=qid, kind=kind, app="core", concern="x", status=status)


class TestAwaitingBuckets(unittest.TestCase):
    def test_an_open_quest_awaits_a_charter(self):
        buckets = _awaiting_buckets([_quest("Q1-Core-A", "OPEN")])
        (entry,) = buckets["idea"]
        assert entry[0] == "Q1-Core-A" and "charter" in entry[2]

    def test_a_planned_quest_awaits_the_dispatch_queue(self):
        buckets = _awaiting_buckets([_quest("Q1-Core-A", "PLANNED")])
        (entry,) = buckets["idea"]
        assert "enqueue" in entry[2]

    def test_pending_ui_review_routes_to_design_not_implementation(self):
        buckets = _awaiting_buckets(
            [_quest("Q1-Core-A", "READY_TO_RAZE")],
            ui_pending_ids={"Q1-Core-A"},
        )
        assert [e[0] for e in buckets["design"]] == ["Q1-Core-A"]
        assert buckets["impl"] == []

    def test_a_verified_promotion_awaits_raze(self):
        buckets = _awaiting_buckets([_quest("Q1-Core-A", "READY_TO_RAZE")])
        (entry,) = buckets["impl"]
        assert "court raze" in entry[2]

    def test_gate_and_tribute_ready_are_in_flight_not_dams(self):
        buckets = _awaiting_buckets([
            _quest("Q1-Core-A", "GATE"),
            _quest("Q2-Core-B", "TRIBUTE_READY"),
        ])
        assert {e[0] for e in buckets["watch"]} == {"Q1-Core-A", "Q2-Core-B"}
        assert buckets["design"] == [] and buckets["impl"] == [] and buckets["idea"] == []

    def test_held_and_punished_are_blocked_side_state(self):
        buckets = _awaiting_buckets([
            _quest("Q1-Core-A", "HELD"),
            _quest("Q2-Core-B", "PUNISHED"),
        ])
        assert {e[0] for e in buckets["blocked"]} == {"Q1-Core-A", "Q2-Core-B"}

    def test_epics_are_not_board_entries(self):
        buckets = _awaiting_buckets([_quest("E1-Core-Epic", "PLANNED", kind="epic")])
        assert all(rows == [] for rows in buckets.values())

    def test_every_quest_lands_in_exactly_one_bucket(self):
        quests = [
            _quest("Q1-Core-A", "OPEN"),
            _quest("Q2-Core-B", "WORKING"),
            _quest("Q3-Core-C", "GATE"),
            _quest("Q4-Core-D", "READY_TO_RAZE"),
        ]
        buckets = _awaiting_buckets(quests)
        placed = [qid for rows in buckets.values() for qid, _, _ in rows]
        assert sorted(placed) == ["Q1-Core-A", "Q2-Core-B", "Q3-Core-C", "Q4-Core-D"]
