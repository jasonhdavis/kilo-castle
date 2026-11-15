"""Bounded drain priority for Coin and collect under dispatch pressure (Q1569).

Ported from the PB App engine repair (commit 5d19ad8536). The 2026-11-15 host
journal shows the rolling hourly spawn budget being spent by the dispatch /
charter / hand-off lanes early in each hour and every queued drain action then
skipped until the hour rolled::

    ⏸️  hourly spawn budget reached (8/h) — skipping collect Q1460-...
    ⏸️  hourly spawn budget reached (8/h) — skipping coin Q1495-...

while dozens of TRIBUTE_READY and GATE Quests waited. The planner is a pure
function, so each scenario is reproduced by driving ``court.nightwatch.plan``
across ticks and spending the budget the way ``court.nightwatch.tick`` does.

Four behaviours are pinned here, each of which FAILS on the pre-port planner and
passes on the ported one:

* (a) an exhausted rolling hourly budget — plan() no longer hands the hour's
  last slots to a hand-off backlog and starves a plannable collect/coin;
* (b) the reserved drain share is bounded (<= half the cap) and spent FIRST;
* (c) a single free worker slot still reaches the drain lane within the
  documented deferral bound;
* (d) the rolling-hour prune keeps ``state['drain_spawns']`` in step with
  ``state['spawns']``, and the executor records drain spawns.
"""
import time

from court import nightwatch as nw
from court import vigils

NOW = 1_000_000.0
TICK_SECONDS = 360  # the supervisor's tick cadence

# The documented one-free-slot bound (a drain action within two ticks, i.e. at
# most one deferral). The getattr keeps this file importable against a pre-port
# engine, where the missing bound is itself the defect.
_ONE_SLOT_MAX_DEFERRALS = getattr(nw, "ONE_SLOT_DRAIN_MAX_DEFERRALS", 1)


def _q(qid, status, worktree="/tmp/nw-drain-wt", worker="idle", **kw):
    base = {
        "id": qid,
        "kind": "quest",
        "status": status,
        "worktree": worktree,
        "worker": {"state": worker},
        "gatekeeper_session_id": "",
        "cogship_id": "",
        "pending_audience": False,
    }
    base.update(kw)
    return base


def _snap(*quests):
    return {"quests": list(quests)}


def _free(tmp_path, name="wt"):
    p = tmp_path / name
    p.mkdir(exist_ok=True)
    return str(p)


def _workers(count=0):
    return {"count": count, "rss_mb": 0.0, "by_role": {}, "workers": [], "dirs": {}}


def _policy(**kw):
    base = dict(max_workers=6, max_spawns_per_tick=3, max_spawns_per_hour=8)
    base.update(kw)
    return nw.Policy(**base)


def _vigil(qid, then=None):
    return vigils.build(
        id=f"vig-{qid}",
        when=[f"quest-status:{qid}=DONE"],
        then=then or f"dispatch:{qid}",
        reason="hand-off registered by the Steward",
        expires="6h",
    )


def _dispatch_vigils(n):
    return [(_vigil(f"QD{i}"), "ready") for i in range(n)]


def _is_drain(action):
    return nw._action_spawns(action) and nw._spawn_class(action) in nw.DRAIN_KINDS


def _is_spawn(action):
    return nw._action_spawns(action)


def _spent_within_hour(count, now, *, drain=0):
    """State preloaded with ``count`` spawn stamps inside the rolling hour."""
    return {
        "spawns": [now - 600 - i for i in range(count)],
        "drain_spawns": [now - 600 - i for i in range(drain)],
    }


def _spend(actions, state, policy, now, *, on_skip="continue"):
    """Mirror ``tick``'s execution loop: spend the hourly budget in plan order.

    Returns ``(executed, skipped)``. The hourly guard matches the executor: a
    spawning action over budget is skipped; ``on_skip='continue'`` (the ported
    behaviour) lets the zero-cost local actions behind it still run, while
    ``on_skip='break'`` reproduces the pre-port skip loop."""
    spawns = [float(t) for t in (state.get("spawns") or ()) if now - float(t) < 3600]
    executed, skipped = [], []
    for action in actions:
        if nw._action_spawns(action):
            if len(spawns) >= policy.max_spawns_per_hour:
                skipped.append(action)
                if on_skip == "break":
                    break
                continue
            spawns.append(now)
            state["spawns"] = list(spawns)
            if nw._spawn_class(action) in nw.DRAIN_KINDS:
                state.setdefault("drain_spawns", []).append(now)
        executed.append(action)
    return executed, skipped


# ---------------------------------------------------------------------------
# (a) An exhausted rolling hourly budget must not starve a plannable drain.
# ---------------------------------------------------------------------------

def test_hourly_budget_exhaustion_does_not_starve_a_plannable_coin(tmp_path):
    """4 of a 5/hour budget spent; one slot left. Pre-port the hand-off vigils
    are planned ahead of the coin and spend that last slot, so the coin is
    skipped as "hourly spawn budget reached (5/h)"."""
    state = nw.load_state(tmp_path)
    state.update(_spent_within_hour(4, NOW))
    policy = _policy(max_workers=6, max_spawns_per_tick=2, max_spawns_per_hour=5)
    snap = _snap(_q("QTR", "TRIBUTE_READY", _free(tmp_path, "w1")))

    actions, _ = nw.plan(snap, state, policy, _workers(), now=NOW,
                         ready_vigils=_dispatch_vigils(2))
    executed, _ = _spend(actions, state, policy, NOW)
    assert any(a.kind == "coin" for a in executed), (
        "a coin was plannable with an hourly slot left but the hand-off lane "
        f"spent it: planned={[a.kind for a in actions]}")


def test_hourly_budget_exhaustion_does_not_starve_a_plannable_collect(tmp_path):
    """The same guarantee for the collect lane (both are DRAIN_KINDS)."""
    state = nw.load_state(tmp_path)
    state.update(_spent_within_hour(4, NOW))
    policy = _policy(max_workers=6, max_spawns_per_tick=2, max_spawns_per_hour=5,
                     collect_min_batch=1)
    snap = _snap(_q("QG", "GATE", _free(tmp_path, "g1")))

    actions, _ = nw.plan(snap, state, policy, _workers(), now=NOW,
                         ready_vigils=_dispatch_vigils(2))
    executed, _ = _spend(actions, state, policy, NOW)
    assert any(a.kind == "collect" for a in executed), (
        "a collect was plannable with an hourly slot left but the hand-off lane "
        f"spent it: planned={[a.kind for a in actions]}")


def test_a_spent_hour_plans_no_spawn_actions(tmp_path):
    """Planning a spawn the hour cannot pay for only produces execution-time
    skip lines (and the old skip loop dropped everything behind them)."""
    state = nw.load_state(tmp_path)
    state.update(_spent_within_hour(8, NOW))
    policy = _policy(max_spawns_per_tick=3, max_spawns_per_hour=8)
    snap = _snap(_q("QTR", "TRIBUTE_READY", _free(tmp_path, "w1")))

    actions, _ = nw.plan(snap, state, policy, _workers(), now=NOW,
                         ready_vigils=_dispatch_vigils(2))
    assert [a.kind for a in actions if _is_spawn(a)] == [], (
        "the tick budget must be capped by the hour's remaining slots: "
        f"{[(a.kind, a.quest_ids) for a in actions if _is_spawn(a)]}")


# ---------------------------------------------------------------------------
# (b) The drain reserve is bounded (<= half the cap) and spent FIRST.
# ---------------------------------------------------------------------------

def test_the_drain_reserve_is_bounded_and_spent_first(tmp_path):
    """With a full hour the retained spawning actions lead with the drain lane,
    and the reserved share never exceeds half the cap — so dispatch is not parked
    behind drain work either."""
    assert 0 < nw.DRAIN_HOURLY_RESERVE_SHARE <= 0.5
    state = nw.load_state(tmp_path)
    policy = _policy(max_workers=6, max_spawns_per_tick=3, max_spawns_per_hour=8)
    snap = _snap(_q("QTR", "TRIBUTE_READY", _free(tmp_path, "w1")))

    actions, _ = nw.plan(snap, state, policy, _workers(), now=NOW,
                         ready_vigils=_dispatch_vigils(3))
    spawns = [a for a in actions if _is_spawn(a)]
    assert spawns, "expected spawn actions for a full hour"
    assert _is_drain(spawns[0]), (
        "the drain lane must spend the reserved share first: "
        f"{[a.kind for a in spawns]}")
    assert any(not _is_drain(a) for a in spawns), (
        "the bounded reserve must still leave dispatch room on a full hour: "
        f"{[a.kind for a in spawns]}")


def test_the_reserve_caps_the_tick_budget_to_the_hours_remaining_slots(tmp_path):
    """1 of a 2/hour budget left: at most one spawn is planned even though the
    per-tick budget is 2."""
    state = nw.load_state(tmp_path)
    state.update(_spent_within_hour(1, NOW))
    policy = _policy(max_workers=6, max_spawns_per_tick=2, max_spawns_per_hour=2)
    snap = _snap(_q("QTR", "TRIBUTE_READY", _free(tmp_path, "w1")))

    actions, _ = nw.plan(snap, state, policy, _workers(), now=NOW,
                         ready_vigils=_dispatch_vigils(2))
    planned = [a for a in actions if _is_spawn(a)]
    assert len(planned) <= 1, f"the hour had one slot but {len(planned)} spawns were planned"


# ---------------------------------------------------------------------------
# (c) One free worker slot must still reach the drain lane within two ticks.
# ---------------------------------------------------------------------------

def test_a_single_free_slot_reaches_the_drain_lane_within_the_deferral_bound(tmp_path):
    """capacity == 1: pre-port the half-pool reserve needed capacity >= 2, so the
    queue-driven dispatch vigil took the single slot on every tick and the coin
    was never even planned (capacity 0 by the time the drain heuristic ran)."""
    policy = _policy(max_workers=3, max_spawns_per_tick=3, max_spawns_per_hour=8)
    state = nw.load_state(tmp_path)
    snap = _snap(_q("QTR", "TRIBUTE_READY", _free(tmp_path, "w1")))

    planned = []
    for tick in range(4):
        actions, _ = nw.plan(snap, state, policy, _workers(count=2),
                             now=NOW + tick * TICK_SECONDS,
                             ready_vigils=_dispatch_vigils(1))
        planned.append(actions)

    first_drain = next(
        (t for t, acts in enumerate(planned) if any(_is_drain(a) for a in acts)),
        None,
    )
    assert first_drain is not None, "the drain lane was never planned in four ticks"
    assert first_drain <= _ONE_SLOT_MAX_DEFERRALS, (
        f"drain work waited {first_drain} ticks for the single free slot "
        f"(bound is {_ONE_SLOT_MAX_DEFERRALS})")
    # Forward progress for dispatch survives the hold.
    assert any(any(not _is_drain(a) for a in acts if _is_spawn(a)) for acts in planned), (
        "the one-slot drain hold parked dispatch for every tick")


# ---------------------------------------------------------------------------
# (d) Rolling-hour prune + drain-spawn accounting.
# ---------------------------------------------------------------------------

def test_the_rolling_hour_prune_resets_drain_spawns_with_spawns():
    state = {"spawns": [NOW - 7200.0, NOW - 60.0],
             "drain_spawns": [NOW - 7200.0, NOW - 120.0]}
    kept = nw._prune_spawns(state, NOW)
    assert kept == [NOW - 60.0]
    assert state["drain_spawns"] == [NOW - 120.0], (
        "a stale drain stamp would hold a reservation the hour no longer owes")


def test_record_spawn_charges_drain_actions_to_both_budgets():
    record = getattr(nw, "_record_spawn", None)
    assert record is not None, (
        "the engine must record drain spawns so the hourly reservation settles")
    state = {}
    record(state, nw.Action(kind="coin", quest_ids=["Q1"]), NOW)
    record(state, nw.Action(kind="collect", quest_ids=["Q2"]), NOW)
    record(state, nw.Action(kind="vigil", quest_ids=[],
                            meta={"then": "dispatch:Q3"}), NOW)
    assert len(state["spawns"]) == 3
    assert len(state["drain_spawns"]) == 2


# ---------------------------------------------------------------------------
# Executor: a skipped over-budget spawn must not abort local work behind it.
# ---------------------------------------------------------------------------

def test_a_skipped_over_budget_spawn_does_not_abort_local_work(tmp_path, monkeypatch):
    """The executor's hourly guard used to `break`, silently dropping every
    zero-cost local action behind an over-budget spawn. It now `continue`s."""
    over = nw.Action(kind="coin", quest_ids=["QTR"], reason="hour already spent")
    local = nw.Action(kind="rebase", quest_ids=["QG"], reason="mechanical rebase",
                      meta={"retry_backoff": 30})

    monkeypatch.setattr(nw, "snapshot", lambda repo, timeout=None: {"quests": []})
    monkeypatch.setattr(nw, "live_kilo_workers", lambda repo: _workers())
    monkeypatch.setattr(nw, "fold_ledger", lambda repo, say=print: True)
    monkeypatch.setattr(vigils, "ready_vigils", lambda repo, qmap, now=None: ([], []))
    monkeypatch.setattr(nw, "plan", lambda *a, **k: ([over, local], []))
    monkeypatch.setattr(nw, "save_state", lambda repo, state: None)
    monkeypatch.setattr(nw, "_journal", lambda repo, event: None)
    ran = []
    monkeypatch.setattr(
        nw, "execute",
        lambda repo, action: ran.append(action.kind)
        or {"ok": True, "exit": 0, "seconds": 0.0})

    state = nw.load_state(tmp_path)
    state["spawns"] = [time.time()] * 50
    nw.tick(tmp_path, _policy(max_spawns_per_hour=50), state, say=lambda *a, **k: None)
    assert ran == ["rebase"], (
        "a skipped over-budget spawn must not abort local work behind it")
