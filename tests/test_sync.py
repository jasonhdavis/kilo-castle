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


def _stamped_charter(status, updated_at, ledger_bullet, tribute=""):
    q = Quest(id="Q1049-Stamp", title="Stamp", app="crm", concern="stamp", status=status)
    q.updated_at = updated_at
    q.set_section("Castle Ledger", ledger_bullet)
    if tribute:
        q.set_section("Tribute Rendered", tribute)
    return q


def test_stamp_side_state_keeps_branch_tribute_and_takes_only_status():
    from court.cli import _stamp_side_state

    castle = _stamped_charter(
        "HELD", "2026-10-31T19:48:01Z",
        "- **2026-10-31T19:48:01Z** — GATE → HELD: UI studio not approved",
    )
    branch = _stamped_charter(
        "TRIBUTE_READY", "2026-11-02T14:02:45Z",
        "- **2026-11-02T14:02:45Z** — HELD → TRIBUTE_READY: UI approved by M'Lord",
        tribute="### Tribute Rendered — annotation pass 10\n\n**Ballad.** Enrichment preview.",
    ).to_markdown()

    stamped = _stamp_side_state(branch, castle)

    assert "status: HELD" in stamped                      # royal side-state applied
    assert "annotation pass 10" in stamped                # branch Tribute survives
    assert "UI approved by M'Lord" in stamped             # branch transition record survives
    assert "UI studio not approved" in stamped            # castle ledger bullet carried over


def test_last_ledger_ts_detects_stale_side_state():
    from court.cli import _last_ledger_ts

    stale = _stamped_charter(
        "HELD", "2026-10-31T19:48:01Z",
        "- **2026-10-31T19:48:01Z** — GATE → HELD: UI studio not approved",
    )
    newer = _stamped_charter(
        "TRIBUTE_READY", "2026-11-02T14:02:45Z",
        "- **2026-11-02T14:02:45Z** — HELD → TRIBUTE_READY: UI approved by M'Lord",
    )
    assert _last_ledger_ts(newer) > _last_ledger_ts(stale)
