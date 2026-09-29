"""court sync union-merge: mixed paperwork forks resolve per the conflict
policy (branch-wins per section, castle-only changes survive, true edit
wars refuse) instead of blanket-refusing."""
from court.cli import _union_events, _union_merge_quest
from court.models import Quest


def _charter(status="WORKING", serf="kilo-serf-96269", goal="Original requirement.",
             tribute=""):
    q = Quest(id="Q700-Union", title="Union", app="test", concern="union", status=status)
    q.serf_session_id = serf
    q.set_section("The Kingdom Requires", goal)
    if tribute:
        q.set_section("Tribute Rendered", tribute)
    return q.to_markdown()


def test_castle_stamp_plus_branch_tribute_unites():
    # base -> castle re-stamped the serf session (dispatch stamps)
    base = _charter()
    castle = _charter(serf="kilo-serf-51460")
    # branch rendered tribute paperwork (serf self-advance)
    branch = _charter(status="TRIBUTE_READY", tribute="- [x] All tests pass")
    merged, conflicts, disclosures = _union_merge_quest(base, castle, branch)
    assert merged is not None and not conflicts
    assert "kilo-serf-51460" in merged          # castle dispatch stamp kept
    assert "All tests pass" in merged           # branch tribute kept
    assert "TRIBUTE_READY" in merged            # branch self-advance kept
    assert "Original requirement." in merged    # untouched section intact


def test_true_edit_war_refuses():
    base = _charter(goal="Original requirement.")
    castle = _charter(goal="M'Lord amended the requirement on castle.")
    branch = _charter(goal="Serf rewrote the requirement on branch.")
    merged, conflicts, _ = _union_merge_quest(base, castle, branch)
    assert merged is None
    assert "The Kingdom Requires" in conflicts


def test_union_events_orders_by_ts_branch_wins_ties():
    b = '\n'.join([
        '{"target": "field:status", "ts": "2026-09-26T10:00:00Z", "value": "WORKING"}',
        '{"target": "field:status", "ts": "2026-09-26T12:00:00Z", "value": "TRIBUTE_READY"}',
    ])
    c = '\n'.join([
        '{"target": "field:serf_session_id", "ts": "2026-09-26T12:00:00Z", "value": "kilo-serf-51460"}',
        '{"target": "field:status", "ts": "2026-09-26T11:00:00Z", "value": "WORKING"}',
    ])
    merged = _union_events(b, c)
    lines = [ln for ln in merged.splitlines() if ln.strip()]
    assert len(lines) == 4
    ts = [__import__("json").loads(ln)["ts"] for ln in lines]
    assert ts == sorted(ts)                     # stable ts ordering
    assert any("kilo-serf-51460" in ln for ln in lines)


def test_union_events_no_castle_extras_returns_branch():
    b = '{"target": "field:status", "ts": "2026-09-26T10:00:00Z", "value": "WORKING"}'
    assert _union_events(b, b) == b
    assert _union_events(b, "") == b
