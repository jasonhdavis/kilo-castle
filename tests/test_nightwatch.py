"""Unit tests for the Night's Watch planner (court.nightwatch.plan).

The planner is pure: snapshot + state + policy + worker census in, actions out.
No repo, no subprocess, no clock.
"""
import json
import time
from pathlib import Path

from court import nightwatch as nw


def _q(qid, status, worktree="/tmp/nw-wt", worker="idle", **kw):
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


def _policy(**kw):
    base = dict(max_workers=3, max_spawns_per_tick=2, max_spawns_per_hour=12)
    base.update(kw)
    return nw.Policy(**base)


def _workers(count=0, dirs=None):
    return {"count": count, "rss_mb": 0.0, "by_role": {}, "workers": [], "dirs": dirs or {}}


def test_goads_a_working_quest_with_a_dead_worker(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "WORKING", wt, worker="exited"))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers())
    assert [(a.kind, a.quest_ids) for a in actions] == [("goad", ["Q1"])]
    assert not [n for n in notes if n["level"] == "human"]


def test_does_not_goad_a_live_or_watching_worker(tmp_path):
    wt = _free(tmp_path)
    for state in ("live", "watching"):
        snap = _snap(_q("Q1", "WORKING", wt, worker=state))
        actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers())
        assert actions == [], state


def test_does_not_goad_beneath_a_live_process_in_the_worktree(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "WORKING", wt, worker="idle"))
    workers = _workers(count=1, dirs={str(Path(wt).resolve()): ["artist"]})
    actions, notes = nw.plan(snap, nw.load_state(tmp_path), _policy(), workers)
    assert actions == []
    assert any(n["level"] == "wait" and "artist" in n["text"] for n in notes)


def test_goad_budget_falls_over_to_dispatch_then_bubbles_to_a_steward(tmp_path):
    """A serf that spends its goads is replaced by a dispatched serf; only when
    every serf slot is spent does the Quest bubble up to a Steward."""
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "WORKING", wt, worker="exited"))
    state = nw.load_state(tmp_path)
    pol = _policy(goads_per_serf=3, serf_slots=2)

    state["quests"]["Q1"] = {"goads": 3}
    actions, notes = nw.plan(snap, state, pol, _workers())
    assert [a.kind for a in actions] == ["dispatch"]
    assert actions[0].meta["serf_index"] == 0

    state["quests"]["Q1"] = {"goads": 6, "serf_index": 1}
    actions, notes = nw.plan(snap, state, pol, _workers())
    assert actions == []
    assert any(n["level"] == "human" and "bubbling up to a Steward" in n["text"] for n in notes)


def test_context_resume_vs_cold_start_on_idle_worker(tmp_path, monkeypatch):
    """An idle worker under the cull line continues the SAME session; only a
    bloated (red-zone) session cold-starts. No deterministic fresh spawn."""
    wt = _free(tmp_path)
    pol = _policy(cull_soft=350_000, hard_red_zone=750_000)
    snap = _snap(_q("Q1", "WORKING", wt, worker="exited", serf_session_id="ses_x"))

    monkeypatch.setattr(nw.handoff, "session_context_tokens", lambda sid: 20_000)
    monkeypatch.setattr(nw.handoff, "worktree_is_clean", lambda wt: True)
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), pol, _workers())
    assert [(a.kind, a.meta["mode"]) for a in actions] == [("goad", "resume")]

    monkeypatch.setattr(nw.handoff, "session_context_tokens", lambda sid: 800_000)
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), pol, _workers())
    assert [(a.kind, a.meta["mode"]) for a in actions] == [("goad", "cold")]


def test_goad_cooldown_suppresses_a_repeat(tmp_path):
    wt = _free(tmp_path)
    state = nw.load_state(tmp_path)
    state["quests"]["Q1"] = {"goads": 1, "last_goad_ts": time.time()}
    snap = _snap(_q("Q1", "WORKING", wt, worker="exited"))
    actions, _ = nw.plan(snap, state, _policy(), _workers())
    assert actions == []


def test_worker_ceiling_blocks_spawns(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "WORKING", wt, worker="exited"))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path), _policy(max_workers=3), _workers(count=3))
    assert actions == []
    assert any("ceiling" in n["text"] for n in notes)


def test_coins_tribute_ready_but_not_while_a_master_of_coin_is_live(tmp_path):
    wt = _free(tmp_path)
    Path(wt, ".kilo", "watch").mkdir(parents=True)
    live_pid = __import__("os").getpid()
    Path(wt, ".kilo", "watch", "master_of_coin.lease.json").write_text(
        '{"v":1,"pending":false,"pid":%d,"pgid":null,"session_id":"ses_x",'
        '"watchdog_pid":null,"holder":"coin","started":0}' % live_pid
    )
    snap = _snap(_q("Q1", "TRIBUTE_READY", wt))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers())
    assert actions == []


def test_coins_tribute_ready_when_idle(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "TRIBUTE_READY", wt))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers())
    assert [(a.kind, a.quest_ids) for a in actions] == [("coin", ["Q1"])]


def test_groups_unstamped_gate_quests_into_one_convoy(tmp_path):
    wt1, wt2 = _free(tmp_path, "a"), _free(tmp_path, "b")
    snap = _snap(_q("Q1", "GATE", wt1), _q("Q2", "GATE", wt2))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers())
    assert len(actions) == 1
    assert actions[0].kind == "collect"
    assert sorted(actions[0].quest_ids) == ["Q1", "Q2"]
    assert actions[0].cogship == ""


def test_gate_with_a_dead_recorded_gatekeeper_is_flagged_not_respawned(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "GATE", wt, gatekeeper_session_id="ses_dead", cogship_id="cogship-9"))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers())
    assert actions == []
    assert any(n["level"] == "human" for n in notes)


def test_pending_audience_parks_everything(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(
        _q("Q1", "TRIBUTE_READY", wt, pending_audience=True),
        _q("Q2", "WORKING", wt, worker="exited", pending_audience=True),
    )
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers())
    assert actions == []


def test_disabled_actions_are_never_planned(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "TRIBUTE_READY", wt), _q("Q2", "WORKING", wt, worker="exited"))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(enabled=("collect",)), _workers())
    assert actions == []


def test_epics_are_ignored(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("E1", "WORKING", wt, kind="epic", worker="exited"))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers())
    assert actions == []


def test_actions_are_capped_per_tick(tmp_path):
    wt = _free(tmp_path)
    quests = [_q(f"Q{i}", "TRIBUTE_READY", _free(tmp_path, f"w{i}")) for i in range(5)]
    actions, _ = nw.plan(_snap(*quests), nw.load_state(tmp_path),
                         _policy(max_workers=10, max_spawns_per_tick=2), _workers())
    assert len(actions) == 2


def test_plan_never_mutates_state(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "WORKING", wt, worker="exited"), _q("Q2", "TRIBUTE_READY", wt))
    state = nw.load_state(tmp_path)
    before = json.dumps(state, sort_keys=True)
    nw.plan(snap, state, _policy(max_workers=10), _workers())
    assert json.dumps(state, sort_keys=True) == before


def test_bookkeeping_moves_only_after_an_attempt(tmp_path):
    wt = _free(tmp_path)
    state = nw.load_state(tmp_path)
    action = nw.Action(kind="goad", quest_ids=["Q1"], meta={"goads_before": 0})
    nw._apply_bookkeeping(state, action, {"ok": False, "stderr_tail": "boom"}, 1000.0)
    assert state["quests"]["Q1"]["goads"] == 1          # failed attempt still counts
    assert state["quests"]["Q1"]["fails"] == 1
    assert "boom" in state["quests"]["Q1"]["last_error"]
    nw._apply_bookkeeping(state, action, {"ok": True, "stderr_tail": ""}, 2000.0)
    assert state["quests"]["Q1"]["fails"] == 0
    assert state["quests"]["Q1"]["last_goad_ts"] == 2000.0


def test_coin_ceiling_flags_a_human(tmp_path):
    wt = _free(tmp_path)
    state = nw.load_state(tmp_path)
    state["quests"]["Q1"] = {"coins": 3}
    snap = _snap(_q("Q1", "TRIBUTE_READY", wt))
    actions, notes = nw.plan(snap, state, _policy(max_workers=10, max_coins=3), _workers())
    assert actions == []
    assert any(n["level"] == "human" for n in notes)


def test_bookkeeping_counts_coin_attempts(tmp_path):
    state = nw.load_state(tmp_path)
    action = nw.Action(kind="coin", quest_ids=["Q1"])
    nw._apply_bookkeeping(state, action, {"ok": True, "stderr_tail": ""}, 500.0)
    nw._apply_bookkeeping(state, action, {"ok": True, "stderr_tail": ""}, 900.0)
    assert state["quests"]["Q1"]["coins"] == 2
    assert state["quests"]["Q1"]["last_coin_ts"] == 900.0


# ---------------------------------------------------------------------------
# Vigils — Steward hand-off
# ---------------------------------------------------------------------------

def _vigil(then="dispatch:Q234", when=None, **kw):
    from court import vigils
    base = dict(id="vig1", when=when or ["quest-status:Q123=DONE"], then=then,
                reason="hand-off registered by the Steward", expires="6h")
    base.update(kw)
    return vigils.build(**base)


def test_a_ready_vigil_becomes_an_action_and_consumes_capacity(tmp_path):
    spec = _vigil()
    snap = _snap(_q("Q234", "CHARTERED"))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(max_workers=2), _workers(),
                         ready_vigils=[(spec, "Q123 is DONE")])
    assert [a.kind for a in actions] == ["vigil"]
    assert actions[0].quest_ids == ["Q234"]
    assert actions[0].meta["then"] == "dispatch:Q234"


def test_a_spawning_vigil_respects_the_worker_ceiling(tmp_path):
    spec = _vigil()
    snap = _snap(_q("Q234", "CHARTERED"))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path), _policy(max_workers=3),
                             _workers(count=3), ready_vigils=[(spec, "ready")])
    assert actions == []
    assert any("ceiling" in n["text"] for n in notes)


def test_a_note_vigil_is_not_gated_by_the_worker_ceiling(tmp_path):
    spec = _vigil(then="note:Q123 landed, remember to bump the release notes")
    actions, _ = nw.plan(_snap(), nw.load_state(tmp_path), _policy(max_workers=1),
                         _workers(count=9), ready_vigils=[(spec, "ready")])
    assert [a.kind for a in actions] == ["vigil"]
    assert actions[0].meta["spawns"] is False


def test_a_vigil_for_a_disabled_action_is_skipped(tmp_path):
    spec = _vigil(then="coin:Q1", when=["quest-status:Q1=TRIBUTE_READY"])
    actions, notes = nw.plan(_snap(), nw.load_state(tmp_path), _policy(enabled=("goad",)),
                             _workers(), ready_vigils=[(spec, "ready")])
    assert actions == []
    assert any(n["level"] == "skip" for n in notes)


# ---------------------------------------------------------------------------
# Context thresholds — cull line / red zone (soft / hard)
# ---------------------------------------------------------------------------

def test_a_watching_session_in_the_red_zone_is_flagged_for_a_human(tmp_path, monkeypatch):
    wt = _free(tmp_path)
    monkeypatch.setattr(nw.handoff, "session_context_tokens", lambda sid: 800_000)
    snap = _snap(_q("Q1", "WORKING", wt, worker="watching", serf_session_id="ses_x"))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path),
                             _policy(cull_soft=350_000, hard_red_zone=750_000), _workers())
    assert actions == []
    assert any(n["level"] == "human" and "red zone" in n["text"] for n in notes)


def test_a_watching_session_past_the_cull_line_is_flagged(tmp_path, monkeypatch):
    wt = _free(tmp_path)
    monkeypatch.setattr(nw.handoff, "session_context_tokens", lambda sid: 400_000)
    snap = _snap(_q("Q1", "WORKING", wt, worker="watching", serf_session_id="ses_x"))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path),
                             _policy(cull_soft=350_000, hard_red_zone=750_000), _workers())
    assert actions == []
    assert any(n["level"] == "human" and "cull line" in n["text"] for n in notes)


def test_a_watching_session_under_the_cull_line_is_left_alone(tmp_path, monkeypatch):
    wt = _free(tmp_path)
    monkeypatch.setattr(nw.handoff, "session_context_tokens", lambda sid: 100_000)
    snap = _snap(_q("Q1", "WORKING", wt, worker="watching", serf_session_id="ses_x"))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path),
                             _policy(cull_soft=350_000, hard_red_zone=750_000), _workers())
    assert actions == []
    assert not [n for n in notes if n["level"] == "human"]


def test_bloat_cold_start_with_force_permission_supersedes_the_watchdog(tmp_path, monkeypatch):
    wt = _free(tmp_path)
    monkeypatch.setattr(nw.handoff, "session_context_tokens", lambda sid: 800_000)
    snap = _snap(_q("Q1", "WORKING", wt, worker="watching", serf_session_id="ses_x"))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path),
                         _policy(cull_soft=350_000, hard_red_zone=750_000,
                                 bloat_cold_start=True, allow_force_goad=True), _workers())
    assert [(a.kind, a.quest_ids) for a in actions] == [("goad", ["Q1"])]
    assert actions[0].force is True


def test_bloat_cold_start_without_force_permission_only_flags(tmp_path, monkeypatch):
    wt = _free(tmp_path)
    monkeypatch.setattr(nw.handoff, "session_context_tokens", lambda sid: 800_000)
    snap = _snap(_q("Q1", "WORKING", wt, worker="watching", serf_session_id="ses_x"))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path),
                             _policy(cull_soft=350_000, hard_red_zone=750_000,
                                     bloat_cold_start=True, allow_force_goad=False), _workers())
    assert actions == []
    assert any(n["level"] == "human" for n in notes)


def test_an_idle_worker_goads_a_bloated_session_as_a_fresh_session(tmp_path, monkeypatch):
    """A bloated (red-zone) session is cold-started on an idle-worker goad; the
    prior-token count is carried in the action meta so the reason is honest."""
    wt = _free(tmp_path)
    monkeypatch.setattr(nw.handoff, "session_context_tokens", lambda sid: 800_000)
    snap = _snap(_q("Q1", "WORKING", wt, worker="exited", serf_session_id="ses_x"))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path),
                         _policy(cull_soft=350_000, hard_red_zone=750_000), _workers())
    assert [(a.kind, a.quest_ids) for a in actions] == [("goad", ["Q1"])]
    assert actions[0].force is False
    assert actions[0].meta["prior_tokens"] == 800_000


def test_multi_quest_collect_uses_one_comma_joined_positional(monkeypatch, tmp_path):
    """`court collect` is batchable via ONE comma-separated positional; three
    separate positionals are rejected by argparse (the bug the ceiling raise
    exposed on the first real convoy pack)."""
    seen = {}
    monkeypatch.setattr(nw, "_court", lambda repo, *argv, **kw: seen.setdefault("argv", list(argv))
                        or __import__("subprocess").CompletedProcess(argv, 0, "", ""))
    action = nw.Action(kind="collect", quest_ids=["Q1", "Q2", "Q3"], cogship="cogship-9")
    nw.execute(tmp_path, action)
    assert seen["argv"] == ["collect", "Q1,Q2,Q3", "--cogship", "cogship-9", "--standup"]


def test_single_quest_collect_has_no_comma(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(nw, "_court", lambda repo, *argv, **kw: seen.setdefault("argv", list(argv))
                        or __import__("subprocess").CompletedProcess(argv, 0, "", ""))
    nw.execute(tmp_path, nw.Action(kind="collect", quest_ids=["Q7"]))
    assert seen["argv"] == ["collect", "Q7", "--standup"]


# ---------------------------------------------------------------------------
# Failed attempts retry quickly instead of burning the full cooldown
# ---------------------------------------------------------------------------

def test_a_failed_action_sets_a_short_retry_window(tmp_path):
    state = nw.load_state(tmp_path)
    action = nw.Action(kind="coin", quest_ids=["Q1"], meta={"retry_backoff": 300})
    nw._apply_bookkeeping(state, action, {"ok": False, "stderr_tail": "unrecognized arguments"}, 1000.0)
    assert state["quests"]["Q1"]["retry_at"] == 1300.0
    # and the planner honours it
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "TRIBUTE_READY", wt))
    actions, _ = nw.plan(snap, state, _policy(max_workers=10), _workers(), now=1200.0)
    assert actions == []
    actions, _ = nw.plan(snap, state, _policy(max_workers=10), _workers(), now=1301.0)
    assert [a.kind for a in actions] == ["coin"]


def test_a_successful_action_clears_the_retry_window(tmp_path):
    state = nw.load_state(tmp_path)
    action = nw.Action(kind="goad", quest_ids=["Q1"], meta={"goads_before": 0, "retry_backoff": 300})
    nw._apply_bookkeeping(state, action, {"ok": False, "stderr_tail": "boom"}, 1000.0)
    assert state["quests"]["Q1"]["retry_at"] == 1300.0
    nw._apply_bookkeeping(state, action, {"ok": True, "stderr_tail": ""}, 1500.0)
    assert state["quests"]["Q1"]["retry_at"] == 0


def test_a_failed_convoy_pack_does_not_burn_the_gate_cooldown(tmp_path):
    state = nw.load_state(tmp_path)
    action = nw.Action(kind="collect", quest_ids=["Q1", "Q2"], meta={"convoy": "new", "retry_backoff": 300})
    nw._apply_bookkeeping(state, action, {"ok": False, "stderr_tail": "unrecognized arguments"}, 1000.0)
    assert state["convoys"]["new"]["retry_at"] == 1300.0
    assert "last_ts" not in state["convoys"]["new"] or not state["convoys"]["new"].get("last_ts")
    wt1, wt2 = _free(tmp_path, "a"), _free(tmp_path, "b")
    snap = _snap(_q("Q1", "GATE", wt1), _q("Q2", "GATE", wt2))
    actions, _ = nw.plan(snap, state, _policy(max_workers=10), _workers(), now=1301.0)
    assert [a.kind for a in actions] == ["collect"]


# ---------------------------------------------------------------------------
# UI-review hold, no-op collect backoff, one-shot escalation
# ---------------------------------------------------------------------------

def _ui_pending(qid, wt, **kw):
    return _q(qid, "GATE", wt, cogship_id="cogship-382", ui_review="PENDING (Recommend Court Artist)", **kw)


def test_a_ui_pending_convoy_is_escalated_and_never_collected(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_ui_pending("Q1", wt))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers())
    assert actions == []
    assert any(n["level"] == "human" and "Court Artist" in n["text"] for n in notes)


def test_a_ui_pending_convoy_does_not_starve_another_collect(tmp_path):
    """The cogship-382 starvation: the UI-pending convoy must not consume the
    only action slot, so a ready convoy still gets packed."""
    blocked = _free(tmp_path, "blocked")
    ready = _free(tmp_path, "ready")
    snap = _snap(_ui_pending("Q1", blocked), _q("Q2", "GATE", ready, cogship_id="cogship-999"))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(max_spawns_per_tick=1), _workers())
    assert [a.kind for a in actions] == ["collect"]
    assert actions[0].quest_ids == ["Q2"]


def test_a_noop_collect_backs_the_convoy_off_hard(tmp_path):
    state = nw.load_state(tmp_path)
    action = nw.Action(kind="collect", quest_ids=["Q1"], meta={"convoy": "cogship-9", "retry_backoff": 300,
                                                               "noop_backoff": 3600})
    nw._apply_bookkeeping(state, action,
                          {"ok": True, "stdout_tail": "⏭️  Skipped 1 candidate(s) (not ready to pack)"}, 1000.0)
    assert state["convoys"]["cogship-9"]["retry_at"] == 4600.0
    assert not state["convoys"]["cogship-9"].get("last_ts")


def test_a_real_pack_sets_the_gate_cooldown(tmp_path):
    state = nw.load_state(tmp_path)
    action = nw.Action(kind="collect", quest_ids=["Q1"], meta={"convoy": "cogship-9", "noop_backoff": 3600})
    nw._apply_bookkeeping(state, action,
                          {"ok": True, "stdout_tail": "🚢 Stamped 1 Quest(s) onto cogship-9."}, 1000.0)
    assert state["convoys"]["cogship-9"]["last_ts"] == 1000.0
    assert state["convoys"]["cogship-9"]["retry_at"] == 0


def test_escalations_are_new_once_then_repeat(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "GATE", wt, gatekeeper_session_id="ses_dead", cogship_id="cogship-9"))
    state = nw.load_state(tmp_path)
    _, notes = nw.plan(snap, state, _policy(), _workers())
    (stale,) = [n for n in notes if n.get("key") == "stale_gatekeeper"]
    assert stale["new"] is True
    nw._record_escalations(state, notes, 1000.0)
    _, notes2 = nw.plan(snap, state, _policy(), _workers())
    (stale2,) = [n for n in notes2 if n.get("key") == "stale_gatekeeper"]
    assert stale2["new"] is False


def test_dispatch_action_runs_replace(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(nw, "_court", lambda repo, *argv, **kw: seen.setdefault("argv", list(argv))
                        or __import__("subprocess").CompletedProcess(argv, 0, "", ""))
    nw.execute(tmp_path, nw.Action(kind="dispatch", quest_ids=["Q7"], meta={"serf_index": 0}))
    assert seen["argv"] == ["dispatch", "Q7", "--replace"]


def test_goad_action_passes_the_mode(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(nw, "_court", lambda repo, *argv, **kw: seen.setdefault("argv", list(argv))
                        or __import__("subprocess").CompletedProcess(argv, 0, "", ""))
    nw.execute(tmp_path, nw.Action(kind="goad", quest_ids=["Q7"], meta={"mode": "resume"}))
    assert seen["argv"] == ["goad", "Q7", "--mode", "resume"]


def test_tick_survives_a_ready_vigil_note(tmp_path, monkeypatch):
    """Regression: tick() used to append vigil notes to `notes` before `plan`
    assigned it (NameError on any tick with a ready-vigil note)."""
    from court import vigils
    monkeypatch.setattr(nw, "snapshot", lambda repo, timeout=0: {"quests": []})
    monkeypatch.setattr(nw, "live_kilo_workers", lambda repo: _workers())
    monkeypatch.setattr(nw, "save_state", lambda repo, state: None)
    monkeypatch.setattr(nw, "_journal", lambda repo, event: None)
    monkeypatch.setattr(vigils, "ready_vigils",
                        lambda repo, qmap, now=None: ([], [{"id": "v1", "text": "a note"}]))
    monkeypatch.setattr(vigils, "load_all", lambda repo, **kw: [])
    res = nw.tick(tmp_path, _policy(), nw.load_state(tmp_path), dry_run=True, say=lambda *a, **k: None)
    assert any(n["level"] == "vigil" for n in res["notes"])
