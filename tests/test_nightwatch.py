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


def test_unstamped_gate_quests_share_one_convoy_regardless_of_app(tmp_path):
    wts = [_free(tmp_path, n) for n in ("a", "b", "c", "d")]
    snap = _snap(
        _q("Q1", "GATE", wts[0], app="marketing"),
        _q("Q2", "GATE", wts[1], app="marketing"),
        _q("Q3", "GATE", wts[2], app="crm"),
        _q("Q4", "GATE", wts[3], app="crm"),
    )
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers())
    collect = [a for a in actions if a.kind == "collect"]
    assert len(collect) == 1
    assert sorted(collect[0].quest_ids) == ["Q1", "Q2", "Q3", "Q4"]
    assert collect[0].cogship == ""


def test_a_lone_gate_quest_waits_for_a_cohort_then_packs_alone(tmp_path):
    wt = _free(tmp_path)
    state = nw.load_state(tmp_path)
    snap = _snap(_q("Q1", "GATE", wt, app="marketing"))

    actions, notes = nw.plan(snap, state, _policy(), _workers(), now=1000.0)
    assert actions == []
    assert any(n.get("key") == "collect_cohort:new" for n in notes)

    # The batching window is a ceiling, not an indefinite hold.
    state["quests"]["Q1"] = {"gate_since_ts": 1000.0}
    actions, _ = nw.plan(snap, state, _policy(), _workers(), now=1000.0 + 1801)
    assert [(a.kind, a.quest_ids, a.cogship) for a in actions] == [("collect", ["Q1"], "")]


def test_collect_min_batch_one_restores_immediate_packing(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "GATE", wt, app="marketing"))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path),
                         _policy(collect_min_batch=1), _workers(), now=1000.0)
    assert [a.kind for a in actions] == ["collect"]


def test_a_cohort_larger_than_the_cap_defers_the_remainder(tmp_path):
    wts = [_free(tmp_path, str(i)) for i in range(7)]
    snap = _snap(*[_q(f"Q{i}", "GATE", wts[i], app="crm") for i in range(7)])
    actions, notes = nw.plan(snap, nw.load_state(tmp_path),
                             _policy(collect_batch_max=3, max_workers=4, max_spawns_per_tick=10),
                             _workers())
    collect = [a for a in actions if a.kind == "collect"]
    assert len(collect) == 1
    assert len(collect[0].quest_ids) == 3
    assert any(n.get("key") == "collect_remainder:new" for n in notes)





def test_gate_with_a_dead_recorded_gatekeeper_is_flagged_not_respawned(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "GATE", wt, gatekeeper_session_id="ses_dead", cogship_id="cogship-9"))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers())
    assert actions == []
    assert any(n["level"] == "human" for n in notes)


def test_a_reshipment_is_recollected_despite_the_old_gatekeeper_stamp(tmp_path):
    """Q1049 class: the Quest shipped once, new work landed after the promotion,
    and the old gatekeeper/session stamps are still on the charter. At the next
    GATE the watch must mint a fresh convoy, not dead-end on `stale_gatekeeper`."""
    wt = _free(tmp_path)
    snap = _snap(_q(
        "Q1", "GATE", wt,
        cogship_id="cogship-9",
        gatekeeper_session_id="ses_old_shipment",
        cogship_promoted_commit="abc1234",
        git_status={"ahead": 45, "behind": 0},
    ))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path), _policy(max_workers=4), _workers())
    assert [a.kind for a in actions] == ["collect"]
    assert actions[0].quest_ids == ["Q1"]
    assert any(n.get("key") == "reship_gate" for n in notes)
    assert not any(n.get("key") == "stale_gatekeeper" for n in notes)


def test_gate_already_merged_with_an_old_stamp_is_not_repacked(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q(
        "Q1", "GATE", wt,
        cogship_id="cogship-9",
        gatekeeper_session_id="ses_old_shipment",
        cogship_promoted_commit="abc1234",
        git_status={"ahead": 0, "behind": 0},
    ))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path), _policy(max_workers=4), _workers())
    assert actions == []
    (note,) = [n for n in notes if n.get("key") == "gate_merged_stuck"]
    assert note["level"] == "human"
    assert "READY_TO_RAZE" in note["text"]


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


def test_raze_bucket_splits_ancestor_from_content_merged_only(tmp_path, monkeypatch):
    from court import nightwatch as _nw
    # Two READY_TO_RAZE quest files; one branch is an ancestor of castle, the
    # other SHA-diverged (content merged but tip not reachable).
    quest_root = tmp_path / ".court" / "quests"
    quest_root.mkdir(parents=True)
    (quest_root / "Q100-Raized.md").write_text(
        "---\nid: Q100-Raized\nstatus: READY_TO_RAZE\nbranch: quest/q100-raized\n---\n", encoding="utf-8")
    (quest_root / "Q101-Regate.md").write_text(
        "---\nid: Q101-Regate\nstatus: READY_TO_RAZE\nbranch: quest/q101-regate\n---\n", encoding="utf-8")

    def fake_find(qid, root):
        p = root / "quests" / f"{qid}.md"
        return p if p.exists() else None

    def fake_merged(branch, target_ref=None, base_ref=None, cwd=None):
        return {"is_merged_target": branch == "quest/q100-raized",
                "is_merged_base": branch == "quest/q100-raized"}

    import court.git_ops as _g
    import court.store as _store
    monkeypatch.setattr(_g, "check_merged_status", fake_merged)
    monkeypatch.setattr(_store, "find_path", fake_find)
    ready, regate = nw._bucket_raze(tmp_path, ["Q100-Raized", "Q101-Regate"])
    assert ready == ["Q100-Raized"]
    assert regate == ["Q101-Regate"]


def test_vigil_dispatch_runs_with_the_watch_dispatch_marker(tmp_path, monkeypatch):
    from court import vigils
    spec = _vigil(then="dispatch:Q234")
    called = {}

    def fake_court(repo, *argv, timeout=None, env=None):
        called["argv"], called["env"] = argv, env
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(nw, "_court", fake_court)
    from court.nightwatch import Action
    res = nw.execute(tmp_path, Action("vigil", quest_ids=["Q234"],
                                      meta={"then": "dispatch:Q234", "spawns": True}))
    assert res["ok"]
    assert called["argv"][0] == "dispatch"
    assert called["env"].get("COURT_WATCH_DISPATCH") == "1"


def test_a_non_spawning_vigil_executes_its_command(tmp_path, monkeypatch):
    """A `rebase:` vigil builds a real argv and must RUN it. The old
    `spawns: false` short-circuit synthesised `ok/exit 0` while running nothing,
    so a queued rebase burned its fires green and converged zero worktrees."""
    called = {}

    def fake_court(repo, *argv, timeout=None, env=None):
        called["argv"] = argv
        class R:
            returncode = 0
            stdout = "🔄 Q1 merged: 12 -> 0 behind."
            stderr = ""
        return R()

    monkeypatch.setattr(nw, "_court", fake_court)
    res = nw.execute(tmp_path, nw.Action("vigil", quest_ids=["Q1"],
                                         meta={"then": "rebase:Q1", "spawns": False}))
    assert list(called["argv"]) == ["rebase", "Q1"]
    assert res["ok"] is True
    assert res["argv"] == ["rebase", "Q1"]


def test_an_advance_vigil_executes_its_command(tmp_path, monkeypatch):
    called = {}

    def fake_court(repo, *argv, timeout=None, env=None):
        called["argv"] = argv
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(nw, "_court", fake_court)
    res = nw.execute(tmp_path, nw.Action("vigil", quest_ids=["Q1"],
                                         meta={"then": "advance:Q1=GATE", "spawns": False}))
    assert list(called["argv"]) == ["advance", "Q1", "GATE"]
    assert res["ok"] is True


def test_a_note_vigil_runs_nothing(tmp_path, monkeypatch):
    sentinel = {}

    def fake_court(repo, *argv, timeout=None, env=None):
        sentinel["argv"] = argv
        raise AssertionError("a note vigil must not invoke court")

    monkeypatch.setattr(nw, "_court", fake_court)
    res = nw.execute(tmp_path, nw.Action("vigil", meta={"then": "note:hello there", "spawns": False}))
    assert res["ok"] is True and res["argv"] == []
    assert "argv" not in sentinel


def test_non_spawning_vigils_are_not_capped_by_the_spawn_budget(tmp_path):
    """Zero-cost local-git actions must not be truncated by the WORKER cap:
    six rebase vigils all survive a max_spawns_per_tick=2 tick."""
    specs = [(_vigil(id=f"r{q}", then=f"rebase:{q}", when=[f"quest-status:{q}=GATE"]), "ready")
             for q in ("QA1", "QA2", "QA3", "QA4", "QA5", "QA6")]
    actions, _ = nw.plan(_snap(), nw.load_state(tmp_path),
                         _policy(max_workers=10, max_spawns_per_tick=2), _workers(),
                         ready_vigils=specs)
    assert len(actions) == 6
    assert all(a.meta["spawns"] is False for a in actions)


def test_non_spawning_vigils_do_not_starve_spawning_actions(tmp_path):
    """The per-tick cap still bounds real spawns; the rebase queue rides free."""
    rebases = [(_vigil(id=f"rb{i}", then=f"rebase:QR{i}", when=[f"quest-status:QR{i}=GATE"]), "ready")
               for i in range(6)]
    quests = [_q(f"QS{i}", "TRIBUTE_READY", _free(tmp_path, f"ws{i}")) for i in range(3)]
    actions, _ = nw.plan(_snap(*quests), nw.load_state(tmp_path),
                         _policy(max_workers=10, max_spawns_per_tick=2), _workers(),
                         ready_vigils=rebases)
    assert sum(1 for a in actions if a.kind == "vigil") == 6   # none dropped
    assert sum(1 for a in actions if a.kind == "coin") == 2    # worker cap intact


def test_action_spawns_classification():
    assert nw._action_spawns(nw.Action("vigil", meta={"then": "rebase:Q1", "spawns": False})) is False
    assert nw._action_spawns(nw.Action("vigil", meta={"then": "coin:Q1", "spawns": True})) is True
    assert nw._action_spawns(nw.Action("rebase", quest_ids=["Q1"])) is False
    assert nw._action_spawns(nw.Action("raze", quest_ids=["Q1"])) is False
    assert nw._action_spawns(nw.Action("coin", quest_ids=["Q1"])) is True
    assert nw._action_spawns(nw.Action("collect", quest_ids=["Q1"])) is True
    assert nw._action_spawns(nw.Action("steward")) is True


def test_a_vigil_for_a_disabled_action_is_skipped(tmp_path):
    spec = _vigil(then="coin:Q1", when=["quest-status:Q1=TRIBUTE_READY"])
    actions, notes = nw.plan(_snap(), nw.load_state(tmp_path), _policy(enabled=("goad",)),
                             _workers(), ready_vigils=[(spec, "ready")])
    assert actions == []
    assert any(n["level"] == "skip" for n in notes)


def test_a_muster_vigil_wakes_one_steward_when_enabled(tmp_path):
    """The Steward's own durable wake-up: a `muster:` vigil becomes a bounded
    `court muster` action when the watch has the steward verb enabled."""
    spec = _vigil(then="muster:clear the stalled Night's Watch board",
                  when=["after:2026-01-01T00:00:00Z"])
    actions, _ = nw.plan(_snap(), nw.load_state(tmp_path),
                         _policy(enabled=("goad", "coin", "collect", "dispatch",
                                          "steward")),
                         _workers(), ready_vigils=[(spec, "ready")])
    assert [a.kind for a in actions] == ["vigil"]
    assert actions[0].meta["then"] == "muster:clear the stalled Night's Watch board"
    assert actions[0].meta["spawns"] is True


def test_a_muster_vigil_is_skipped_unless_the_watch_opted_into_steward(tmp_path):
    spec = _vigil(then="muster:clear the stalled Night's Watch board",
                  when=["after:2026-01-01T00:00:00Z"])
    actions, notes = nw.plan(_snap(), nw.load_state(tmp_path),
                             _policy(enabled=("goad", "coin", "collect", "dispatch")),
                             _workers(), ready_vigils=[(spec, "ready")])
    assert actions == []
    assert any(n["level"] == "skip" and "steward" in n["text"] for n in notes)


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


def test_session_prune_runs_only_on_an_idle_board_once_per_interval(tmp_path):
    """The kilo.db perennial: razing is housekeeping, so it may only run when
    nothing else is happening (VACUUM takes an exclusive lock) and at most once
    per interval."""
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "READY_TO_RAZE", wt, razed_at="2026-11-01T00:00:00Z"))
    state = nw.load_state(tmp_path)
    pol = _policy(max_workers=3, enabled=("prune",),
                  session_prune_interval=86400, session_retention_days=14)

    actions, _ = nw.plan(snap, state, pol, _workers(0), now=100_000.0)
    (prune,) = [a for a in actions if a.kind == "prune"]
    assert prune.meta["days"] == 14
    assert prune.meta["vacuum"] is True

    # A live worker anywhere makes the board non-idle.
    actions, _ = nw.plan(snap, state, pol, _workers(1), now=200_000.0)
    assert not [a for a in actions if a.kind == "prune"]

    # Worker-spawning work this tick also defers it.
    wt2 = _free(tmp_path, "wt2")
    working = _snap(_q("Q1", "READY_TO_RAZE", wt, razed_at="2026-11-01T00:00:00Z"),
                    _q("Q2", "WORKING", wt2, worker="exited"))
    pol2 = _policy(max_workers=3, enabled=("prune", "goad"),
                   session_prune_interval=86400)
    actions, _ = nw.plan(working, state, pol2, _workers(0), now=300_000.0)
    assert [a.kind for a in actions] == ["goad"]

    # The interval is respected after a successful raze.
    state["maintenance"] = {"session_prune_ts": 400_000.0}
    plain = _snap(_q("Q1", "READY_TO_RAZE", wt, razed_at="2026-11-01T00:00:00Z"))
    actions, _ = nw.plan(plain, state, pol, _workers(0), now=400_060.0)
    assert not [a for a in actions if a.kind == "prune"]
    actions, _ = nw.plan(plain, state, pol, _workers(0), now=400_000.0 + 86401)
    assert [a.kind for a in actions if a.kind == "prune"]


def test_a_failed_session_prune_backs_off_and_a_success_stamps_the_interval(tmp_path):
    state = nw.load_state(tmp_path)
    action = nw.Action(kind="prune", quest_ids=[],
                       meta={"days": 14, "retry_backoff": 300})
    nw._apply_bookkeeping(state, action, {"ok": False, "stderr_tail": "database is locked"}, 1000.0)
    maint = state["maintenance"]
    assert maint["session_prune_fails"] == 1
    assert maint["session_prune_retry_at"] == 1300.0
    assert "session_prune_ts" not in maint

    nw._apply_bookkeeping(state, action, {"ok": True, "stdout_tail": "done"}, 2000.0)
    maint = state["maintenance"]
    assert maint["session_prune_ts"] == 2000.0
    assert maint["session_prune_fails"] == 0


def test_a_repeatedly_failing_convoy_backs_off_exponentially(tmp_path):
    """cogship-381/399 storm: identical failures retried every 300s for hours.
    Consecutive failures now double the window (capped) instead of looping."""
    state = nw.load_state(tmp_path)
    action = nw.Action(kind="collect", quest_ids=["Q1"],
                       meta={"convoy": "cogship-381", "retry_backoff": 300,
                             "max_retry_backoff": 3600})
    for expected in (1300.0, 1600.0, 2200.0, 3400.0, 4600.0, 4600.0):
        nw._apply_bookkeeping(state, action, {"ok": False, "stderr_tail": "boom"}, 1000.0)
        assert state["convoys"]["cogship-381"]["retry_at"] == expected
    assert state["convoys"]["cogship-381"]["fails"] == 6


def test_a_convoy_at_the_fail_ceiling_is_escalated_as_a_dead_letter(tmp_path):
    state = nw.load_state(tmp_path)
    state["convoys"]["cogship-381"] = {
        "fails": 3, "retry_at": 0, "last_error": "already exists (prior convoy died)",
    }
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "GATE", wt, cogship_id="cogship-381"))
    actions, notes = nw.plan(snap, state, _policy(max_workers=10), _workers(), now=5000.0)
    (esc,) = [n for n in notes if n.get("key") == "convoy_fails"]
    assert esc["level"] == "human"
    assert "DEAD LETTER" in esc["text"]
    # The convoy still retries at the capped window: fixing the blocker (or a
    # restart) resumes it without a manual state reset.
    assert [a.kind for a in actions] == ["collect"]


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
# UI review is tracked debt, never a GATE hold
# ---------------------------------------------------------------------------

def _ui_pending(qid, wt, **kw):
    return _q(qid, "GATE", wt, cogship_id="cogship-382", ui_review="PENDING (Recommend Court Artist)", **kw)


def test_a_ui_pending_gate_quest_proceeds_to_collect(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_ui_pending("Q1", wt))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers())
    assert [a.kind for a in actions] == ["collect"]
    assert not any(n.get("key") == "ui_pending" for n in notes)


def test_a_ui_pending_convoy_takes_a_regular_collect_slot(tmp_path):
    """The cogship-382 starvation is gone: a PENDING-UI convoy plans collect
    like any other GATE quest instead of eating the escalation slot."""
    blocked = _free(tmp_path, "blocked")
    ready = _free(tmp_path, "ready")
    snap = _snap(_ui_pending("Q1", blocked), _q("Q2", "GATE", ready, cogship_id="cogship-999"))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path), _policy(max_spawns_per_tick=1), _workers())
    assert [a.kind for a in actions] == ["collect"]
    assert actions[0].quest_ids == ["Q1"]
    assert not any(n.get("key") == "ui_pending" for n in notes)


def test_a_gate_quest_with_pending_audience_is_still_parked(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_ui_pending("Q1", wt, pending_audience=True))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers())
    assert actions == []
    assert any(n.get("level") == "skip" and "Audience" in n.get("text", "") for n in notes)


def test_a_noop_collect_backs_the_convoy_off_hard(tmp_path):
    state = nw.load_state(tmp_path)
    action = nw.Action(kind="collect", quest_ids=["Q1"], meta={"convoy": "cogship-9", "retry_backoff": 300,
                                                               "noop_backoff": 3600})
    nw._apply_bookkeeping(state, action,
                          {"ok": True, "stdout_tail": "⏭️  Skipped 1 candidate(s) (not ready to pack)"}, 1000.0)
    assert state["convoys"]["cogship-9"]["retry_at"] == 4600.0
    assert not state["convoys"]["cogship-9"].get("last_ts")


def test_a_live_convoy_defers_collect_without_counting_a_quest_failure(tmp_path):
    state = nw.load_state(tmp_path)
    action = nw.Action(kind="collect", quest_ids=["Q1", "Q2"],
                       meta={"convoy": "new", "retry_backoff": 300, "live_backoff": 3600})
    nw._apply_bookkeeping(state, action, {
        "ok": False,
        "stderr_tail": ("ERROR: convoy cogship-396 is LIVE — its gatehouse worktree "
                        "(/tmp/x/the-gatehouse-cogship-396) is mid-integration"),
    }, 1000.0)
    convoy = state["convoys"]["new"]
    assert convoy["retry_at"] == 4600.0
    assert convoy["live"]["convoy"] == "cogship-396"
    for qid in ("Q1", "Q2"):
        assert not state["quests"][qid].get("fails")
        assert not state["quests"][qid].get("retry_at")
        assert "cogship-396 is LIVE" in state["quests"][qid]["last_error"]


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


# ---------------------------------------------------------------------------
# Steward muster — the opt-in last resort for a stale board
# ---------------------------------------------------------------------------

def _stale(snap, tmp_path, since=1000.0):
    state = nw.load_state(tmp_path)
    state["board_signature"] = nw.board_signature(nw._quest_map(snap), [])
    state["board_sig_since"] = since
    return state


def test_musters_a_steward_when_the_board_is_stale_and_nothing_is_running(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "READY_TO_RAZE", wt))
    state = _stale(snap, tmp_path)
    actions, _ = nw.plan(snap, state,
                         _policy(enabled=("steward",), steward_stale_after=600),
                         _workers(), now=2000.0)
    assert [a.kind for a in actions] == ["steward"]
    assert actions[0].quest_ids == []


def test_does_not_muster_on_a_fresh_board(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "READY_TO_RAZE", wt))
    state = _stale(snap, tmp_path, since=1950.0)
    actions, _ = nw.plan(snap, state,
                         _policy(enabled=("steward",), steward_stale_after=600),
                         _workers(), now=2000.0)
    assert actions == []


def test_does_not_muster_when_a_steward_is_already_live(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "READY_TO_RAZE", wt))
    state = _stale(snap, tmp_path)
    live = _workers()
    live["by_role"] = {"steward": 1}
    actions, _ = nw.plan(snap, state,
                         _policy(enabled=("steward",), steward_stale_after=600),
                         live, now=2000.0)
    assert actions == []

    leased = _workers()
    leased["steward_lease"] = {"state": "live"}
    actions, _ = nw.plan(snap, state,
                         _policy(enabled=("steward",), steward_stale_after=600),
                         leased, now=2000.0)
    assert actions == []


def test_does_not_muster_while_any_worker_is_running(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "READY_TO_RAZE", wt))
    state = _stale(snap, tmp_path)
    actions, _ = nw.plan(snap, state,
                         _policy(enabled=("steward",), steward_stale_after=600),
                         _workers(count=1), now=2000.0)
    assert actions == []


def test_steward_cooldown_suppresses_a_repeat_muster(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "READY_TO_RAZE", wt))
    state = _stale(snap, tmp_path)
    state["steward"] = {"last_ts": 1900.0}
    actions, notes = nw.plan(snap, state,
                             _policy(enabled=("steward",), steward_stale_after=600,
                                     steward_cooldown=3600),
                             _workers(), now=2000.0)
    assert actions == []
    assert any(n["level"] == "wait" for n in notes)


def test_daily_muster_cap_escalates_to_a_human(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "READY_TO_RAZE", wt))
    state = _stale(snap, tmp_path)
    state["steward"] = {"last_ts": 100.0, "spawns": [1900.0, 1950.0]}
    actions, notes = nw.plan(snap, state,
                             _policy(enabled=("steward",), steward_stale_after=600,
                                     steward_cooldown=60, max_stewards_per_day=2),
                             _workers(), now=2000.0)
    assert actions == []
    assert any(n["level"] == "human" and "muster cap" in n["text"] for n in notes)


def test_steward_is_opt_in(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "READY_TO_RAZE", wt))
    state = _stale(snap, tmp_path)
    actions, _ = nw.plan(snap, state,
                         _policy(enabled=("goad", "coin", "collect", "dispatch"),
                                 steward_stale_after=600),
                         _workers(), now=2000.0)
    assert actions == []


def test_board_signature_changes_when_the_pipeline_moves(tmp_path):
    wt = _free(tmp_path)
    a = nw.board_signature(nw._quest_map(_snap(_q("Q1", "WORKING", wt))), [])
    b = nw.board_signature(nw._quest_map(_snap(_q("Q1", "TRIBUTE_READY", wt))), [])
    assert a != b


def test_muster_action_runs_the_court_muster_verb(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(nw, "_court", lambda repo, *argv, **kw: seen.setdefault("argv", list(argv))
                        or __import__("subprocess").CompletedProcess(argv, 0, "", ""))
    nw.execute(tmp_path, nw.Action(kind="steward", reason="board stale"))
    assert seen["argv"] == ["muster", "--why", "board stale"]


def test_muster_bookkeeping_counts_the_spawn_and_retries_short_on_failure(tmp_path):
    state = nw.load_state(tmp_path)
    action = nw.Action(kind="steward", meta={"retry_backoff": 300})
    nw._apply_bookkeeping(state, action, {"ok": False, "stderr_tail": "boom"}, 1000.0)
    assert state["steward"]["retry_at"] == 1300.0
    assert state["steward"]["fails"] == 1
    nw._apply_bookkeeping(state, action,
                          {"ok": True, "stdout_tail": "🧭 Stood up session ses_9 via Kilo CLI"},
                          1500.0)
    assert state["steward"]["retry_at"] == 0
    assert state["steward"]["last_ts"] == 1500.0
    assert state["steward"]["spawns"] == [1500.0]
    assert state["steward"]["last_session"] == "ses_9"


def test_no_muster_while_the_watch_still_has_a_mechanical_move(tmp_path):
    wt1, wt2 = _free(tmp_path, "a"), _free(tmp_path, "b")
    snap = _snap(_q("Q1", "WORKING", wt1, worker="exited"),
                 _q("Q2", "READY_TO_RAZE", wt2))
    state = _stale(snap, tmp_path)
    actions, _ = nw.plan(snap, state,
                         _policy(enabled=("goad", "steward"), steward_stale_after=600),
                         _workers(), now=2000.0)
    assert [a.kind for a in actions] == ["goad"]


# ---------------------------------------------------------------------------
# Stale convoy: a dead convoy's gatehouse worktree still exists
# ---------------------------------------------------------------------------

def test_a_stale_convoy_is_held_and_escalated_once(tmp_path):
    gate = _free(tmp_path, "the-gatehouse-cogship-9")
    wt = _free(tmp_path, "q1")
    state = nw.load_state(tmp_path)
    state["convoys"]["cogship-9"] = {"stale": {"worktree": gate, "reason": "died"}}
    snap = _snap(_q("Q1", "GATE", wt, cogship_id="cogship-9"))
    # Teardown deliberately disabled: the watch may only report/escalate.
    held = _policy(enabled=("goad", "coin", "collect", "dispatch"))
    actions, notes = nw.plan(snap, state, held, _workers())
    assert actions == []
    (note,) = [n for n in notes if n.get("key") == "stale_convoy"]
    assert note["level"] == "human" and note["new"] is True
    assert "already exists" in note["text"] or "stale gatehouse" in note["text"]

    nw._record_escalations(state, notes, 1000.0)
    _, notes2 = nw.plan(snap, state, held, _workers())
    (note2,) = [n for n in notes2 if n.get("key") == "stale_convoy"]
    assert note2["new"] is False


def test_a_stale_convoy_heals_once_its_worktree_is_gone(tmp_path):
    wt = _free(tmp_path, "q1")
    state = nw.load_state(tmp_path)
    state["convoys"]["cogship-9"] = {
        "stale": {"worktree": str(tmp_path / "gone-gatehouse")},
        "retry_at": 9_999_999_999.0,
    }
    nw._heal_stale_convoys(state)
    assert "stale" not in state["convoys"]["cogship-9"]
    assert state["convoys"]["cogship-9"]["retry_at"] == 0
    snap = _snap(_q("Q1", "GATE", wt, cogship_id="cogship-9"))
    actions, _ = nw.plan(snap, state, _policy(), _workers())
    assert [a.kind for a in actions] == ["collect"]


def test_a_stale_gatehouse_failure_sets_a_long_hold(tmp_path):
    state = nw.load_state(tmp_path)
    action = nw.Action(kind="collect", quest_ids=["Q1", "Q2"], cogship="cogship-9",
                       meta={"convoy": "cogship-9", "retry_backoff": 300, "stale_backoff": 3600})
    result = {
        "ok": False, "exit": 1,
        "stdout_tail": "🚢 Stamped 2 Quest(s) onto cogship-9.\n",
        "stderr_tail": "ERROR: Gatehouse worktree /tmp/the-gatehouse-cogship-9 already exists (no fresh session)",
    }
    nw._apply_bookkeeping(state, action, result, 1000.0)
    convoy = state["convoys"]["cogship-9"]
    assert convoy["stale"]["worktree"] == "/tmp/the-gatehouse-cogship-9"
    assert convoy["retry_at"] == 4600.0
    assert "last_ts" not in convoy


def test_digest_names_stuck_convoys_and_separates_noops(tmp_path, monkeypatch):
    state = nw.load_state(tmp_path)
    state["convoys"]["cogship-9"] = {"stale": {"worktree": "/tmp/the-gatehouse-cogship-9"}}
    monkeypatch.setattr(nw, "load_state", lambda repo: state)
    journal = [
        {"ts": "t1", "event": "action", "action": {"kind": "collect", "quest_ids": ["Q1", "Q2"],
         "cogship": "cogship-9", "meta": {"convoy": "cogship-9"}},
         "result": {"ok": False, "exit": 1, "stdout_tail": "🚢 Stamped 2 Quest(s) onto cogship-9.",
                    "stderr_tail": "ERROR: Gatehouse worktree /tmp/the-gatehouse-cogship-9 already exists"}},
        {"ts": "t2", "event": "action", "action": {"kind": "collect", "quest_ids": ["Q3"],
         "cogship": "cogship-7", "meta": {"convoy": "cogship-7"}},
         "result": {"ok": True, "exit": 0, "stdout_tail": "Skipped 2 candidate(s) (not ready to pack)",
                    "stderr_tail": ""}},
        {"ts": "t3", "event": "action", "action": {"kind": "collect", "quest_ids": ["Q3"],
         "cogship": "cogship-7", "meta": {"convoy": "cogship-7"}},
         "result": {"ok": True, "exit": 0, "stdout_tail": "Skipped 2 candidate(s) (not ready to pack)",
                    "stderr_tail": ""}},
    ]
    out = nw.render_digest(tmp_path, journal=journal)
    assert "stuck convoys" in out and "the-gatehouse-cogship-9" in out
    assert "livelock" in out and "cogship-7" in out
    assert "cogship-9: /tmp/the-gatehouse-cogship-9" in out


# ---------------------------------------------------------------------------
# Opt-in raze / teardown help
# ---------------------------------------------------------------------------

def test_raze_is_on_by_default_and_honours_an_explicit_disable(tmp_path):
    """Razing stopped being a human gate on 2026-11-09: promotion is verified by
    `court raze` itself, and the teardown that follows is mechanical. Disabling
    it is still possible, and then the watch reports instead of acting."""
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "READY_TO_RAZE", wt, worker="idle"))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers())
    assert [(a.kind, a.quest_ids) for a in actions] == [("raze", ["Q1"])]

    actions, notes = nw.plan(snap, nw.load_state(tmp_path),
                             _policy(enabled=("goad",)), _workers())
    assert actions == []
    assert any(n["level"] == "report" and "READY_TO_RAZE" in n["text"] for n in notes)


def test_raze_waits_for_a_live_worker(tmp_path):
    wt = _free(tmp_path)
    Path(wt, ".kilo", "watch").mkdir(parents=True)
    import os
    Path(wt, ".kilo", "watch", "serf.lease.json").write_text(
        '{"v":1,"pending":false,"pid":%d,"pgid":null,"session_id":"ses_x",'
        '"watchdog_pid":null,"holder":"serf","started":0}' % os.getpid())
    snap = _snap(_q("Q1", "READY_TO_RAZE", wt, worker="idle"))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path),
                             _policy(enabled=("raze",), max_workers=4), _workers())
    assert actions == []
    assert any(n["level"] == "wait" for n in notes)


def test_raze_action_runs_court_raze(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(nw, "_court", lambda repo, *argv, **kw: seen.setdefault("argv", list(argv))
                        or __import__("subprocess").CompletedProcess(argv, 0, "", ""))
    nw.execute(tmp_path, nw.Action(kind="raze", quest_ids=["Q7"]))
    assert seen["argv"] == ["raze", "Q7"]


def test_execute_records_pack_success_before_truncating_stdout(tmp_path, monkeypatch):
    """The `Stamped` line precedes a long next-steps block, so the truncated
    tail loses it; a multi-quest pack must still count as progress."""
    class R:
        returncode = 0
        stderr = ""
        stdout = "🚢 Stamped 2 Quest(s) onto cogship-9.\n" + ("next-steps line\n" * 300)

    monkeypatch.setattr(nw, "_court", lambda *a, **k: R())
    res = nw.execute(tmp_path, nw.Action(kind="collect", quest_ids=["Q1", "Q2"]))
    assert "Stamped" not in res["stdout_tail"]
    assert res["packed"] is True
    assert nw._collect_packed(res) is True


def test_collect_packed_honours_the_explicit_flag():
    assert nw._collect_packed({"ok": True, "packed": True,
                               "stdout_tail": "no marker here"}) is True
    assert nw._collect_packed({"ok": True, "packed": False,
                               "stdout_tail": "🚢 Stamped 2 Quest(s) onto cogship-9."}) is False



def test_a_razed_quest_gets_one_teardown_and_no_re_verification(tmp_path):
    """The durable razed_at marker distinguishes 'awaiting raze' from 'already
    razed'. The latter only needs the outstanding teardown, so the watch emits
    one idempotent `raze` (which tears down and archives) rather than repeating
    the promotion checks — the Q006/Q014 hourly no-op loop."""
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "READY_TO_RAZE", wt, worker="idle",
                    razed_at="2026-11-02T16:00:00Z"))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path),
                         _policy(max_workers=4), _workers())
    assert [(a.kind, a.quest_ids) for a in actions] == [("raze", ["Q1"])]
    assert "teardown" in actions[0].reason

    # Disabled: the old report-only behaviour, naming the outstanding teardown.
    actions, notes = nw.plan(snap, nw.load_state(tmp_path),
                             _policy(enabled=("goad",)), _workers())
    assert actions == []
    (note,) = [n for n in notes if n.get("key") == "razed_pruned"]
    assert "already razed" in note["text"]


def test_teardown_is_on_by_default_and_planned_when_the_convoy_is_dead(tmp_path):
    gate = _free(tmp_path, "the-gatehouse-cogship-9")
    wt = _free(tmp_path, "q1")
    state = nw.load_state(tmp_path)
    state["convoys"]["cogship-9"] = {"stale": {"worktree": gate}}
    snap = _snap(_q("Q1", "GATE", wt, cogship_id="cogship-9"))

    # disabled: report only, no teardown
    held = _policy(enabled=("goad", "coin", "collect", "dispatch"))
    actions, notes = nw.plan(snap, state, held, _workers())
    assert actions == []
    assert any(n.get("key") == "stale_convoy" for n in notes)

    # default policy: revive the dead gatehouse so collect can re-run
    actions, _ = nw.plan(snap, state, _policy(max_workers=4), _workers())
    assert [a.kind for a in actions] == ["teardown"]
    assert actions[0].meta["worktree"] == gate
    assert actions[0].meta["branch"] == "the-gatehouse/cogship-9"


def test_a_repeatedly_unremovable_gatehouse_escalates_instead_of_looping(tmp_path):
    gate = _free(tmp_path, "the-gatehouse-cogship-9")
    wt = _free(tmp_path, "q1")
    state = nw.load_state(tmp_path)
    state["convoys"]["cogship-9"] = {
        "stale": {"worktree": gate},
        "teardown_fails": 2,
        "teardown_error": "permission denied",
    }
    snap = _snap(_q("Q1", "GATE", wt, cogship_id="cogship-9"))
    actions, notes = nw.plan(snap, state, _policy(max_workers=4), _workers())
    assert actions == []
    (note,) = [n for n in notes if n.get("key") == "stale_convoy_teardown_failed"]
    assert note["level"] == "human"
    assert "worktree remove --force" in note["text"]


def test_teardown_action_removes_worktree_and_branch(tmp_path, monkeypatch):
    calls = []

    class R:
        def __init__(self, rc=0, out="", err=""):
            self.returncode = rc; self.stdout = out; self.stderr = err

    def _run(cmd, **kw):
        calls.append(list(cmd))
        if "remove" in cmd:
            import shutil
            shutil.rmtree(cmd[-1], ignore_errors=True)
        return R()

    monkeypatch.setattr(nw.subprocess, "run", _run)
    gate = tmp_path / ".kilo" / "worktrees" / "the-gatehouse-cogship-9"
    gate.mkdir(parents=True)
    res = nw.execute(tmp_path, nw.Action(kind="teardown", quest_ids=["Q1"],
                                         meta={"worktree": str(gate), "branch": "the-gatehouse/cogship-9"}))
    assert res["ok"] is True
    assert calls[0][:5] == ["git", "-C", str(tmp_path), "worktree", "remove"]
    assert calls[1][:5] == ["git", "-C", str(tmp_path), "branch", "-D"]
    assert calls[1][-1] == "the-gatehouse/cogship-9"


def test_teardown_refuses_a_non_gatehouse_path(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(nw.subprocess, "run", lambda cmd, **kw: calls.append(list(cmd)))
    outside = _free(tmp_path, "some-quest-worktree")
    res = nw.execute(tmp_path, nw.Action(kind="teardown", quest_ids=["Q1"],
                                         meta={"worktree": outside, "branch": "the-gatehouse/cogship-9"}))
    assert res["ok"] is False
    assert "refusing" in res["stderr_tail"]
    assert calls == []


def test_a_dead_gatehouse_is_revived_without_any_gate_quest(tmp_path):
    gate = _free(tmp_path, "the-gatehouse-cogship-9")
    snap = {"quests": [], "gatehouses": [{
        "cogship_id": "cogship-9",
        "branch": "the-gatehouse/cogship-9",
        "path": gate,
        "fresh_session": False,
        "age_seconds": 3600,
    }]}
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(max_workers=4), _workers())
    assert [a.kind for a in actions] == ["teardown"]
    assert actions[0].meta["worktree"] == gate
    assert actions[0].meta["branch"] == "the-gatehouse/cogship-9"


def test_a_brand_new_gatehouse_is_not_torn_down_before_it_can_be_used(tmp_path):
    gate = _free(tmp_path, "the-gatehouse-cogship-9")
    snap = {"quests": [], "gatehouses": [{
        "cogship_id": "cogship-9",
        "branch": "the-gatehouse/cogship-9",
        "path": gate,
        "fresh_session": False,
        "age_seconds": 5,
    }]}
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(max_workers=4), _workers())
    assert actions == []


def test_dead_gatehouse_teardown_is_not_starved_by_the_worker_ceiling(tmp_path):
    """Teardown is a local git operation, not a model worker. A saturated fleet
    must still be able to clear the dead gatehouse blocking collect."""
    gate = _free(tmp_path, "the-gatehouse-cogship-9")
    snap = {"quests": [], "gatehouses": [{
        "cogship_id": "cogship-9",
        "branch": "the-gatehouse/cogship-9",
        "path": gate,
        "fresh_session": False,
        "age_seconds": 3600,
    }]}
    actions, _ = nw.plan(snap, nw.load_state(tmp_path),
                         _policy(max_workers=4, max_spawns_per_tick=2), _workers(count=10))
    assert [a.kind for a in actions] == ["teardown"]


def test_a_gatehouse_with_a_fresh_session_is_never_torn_down(tmp_path):
    gate = _free(tmp_path, "the-gatehouse-cogship-9")
    snap = {"quests": [], "gatehouses": [{
        "cogship_id": "cogship-9",
        "branch": "the-gatehouse/cogship-9",
        "path": gate,
        "fresh_session": True,
    }]}
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(max_workers=4), _workers())
    assert actions == []


def test_teardown_bookkeeping_clears_the_hold(tmp_path):
    state = nw.load_state(tmp_path)
    state["convoys"]["cogship-9"] = {"stale": {"worktree": "/tmp/x"}, "retry_at": 9999}
    nw._apply_bookkeeping(state, nw.Action(kind="teardown", quest_ids=["Q1"],
                                           meta={"convoy": "cogship-9"}),
                          {"ok": True, "exit": 0}, 1000.0)
    assert "stale" not in state["convoys"]["cogship-9"]
    assert state["convoys"]["cogship-9"]["retry_at"] == 0


def test_digest_lists_ready_to_raze(tmp_path, monkeypatch):
    journal = [{"ts": "t1", "event": "tick", "statuses": {"Q1": "READY_TO_RAZE", "Q2": "GATE"}}]
    out = nw.render_digest(tmp_path, journal=journal)
    assert "ready to raze" in out and "Q1" in out


def test_digest_separates_already_razed_quests_awaiting_prune(tmp_path, monkeypatch):
    journal = [{"ts": "t1", "event": "tick",
                "statuses": {"Q1": "READY_TO_RAZE", "Q2": "READY_TO_RAZE"},
                "razed": ["Q2"]}]
    out = nw.render_digest(tmp_path, journal=journal)
    assert "already razed" in out and "Q2" in out
    pending = out.split("already razed", 1)[0]
    assert "Q1" in pending and "Q2" not in pending.split("ready to raze", 1)[-1]


def test_digest_reports_capacity_and_the_dispatch_queue(tmp_path):
    """The Steward coordinates the MacBook by reading the watch's capacity and
    the queued dispatch hand-offs, not by spawning workers directly."""
    from court import vigils
    (tmp_path / ".court" / "vigils").mkdir(parents=True)
    spec = vigils.build(
        id="dispatch-q001",
        when=["after:2020-01-01T00:00:00Z"],
        then="dispatch:Q001",
        reason="queue this for the watch when capacity frees",
        expires="2h",
    )
    vigils.save(tmp_path, spec)
    journal = [
        {"event": "start", "policy": {"max_workers": 3}},
        {"event": "tick", "workers": 1, "rss_mb": 512},
    ]
    out = nw.render_digest(tmp_path, journal=journal)
    assert "capacity: 1/3 live workers" in out
    assert "dispatch queue: 1 hand-off(s)" in out
    assert "dispatch:Q001" in out


def test_repeated_convoy_failures_are_flagged_to_the_steward(tmp_path):
    wt = _free(tmp_path)
    state = nw.load_state(tmp_path)
    state["convoys"]["cogship-9"] = {"fails": 3, "last_error": "boom", "retry_at": 0}
    snap = _snap(_q("Q1", "GATE", wt, cogship_id="cogship-9"))
    actions, notes = nw.plan(snap, state, _policy(), _workers())
    assert any(n["level"] == "human" and n.get("key") == "convoy_fails" for n in notes)


def test_a_hard_collect_failure_increments_the_convoy_fail_counter(tmp_path):
    state = nw.load_state(tmp_path)
    nw._apply_bookkeeping(
        state, nw.Action(kind="collect", quest_ids=["Q1"], cogship="cogship-9",
                         meta={"convoy": "cogship-9", "retry_backoff": 300}),
        {"ok": False, "exit": 2, "stdout_tail": "", "stderr_tail": "boom"}, 1000.0)
    assert state["convoys"]["cogship-9"]["fails"] == 1
    assert "boom" in state["convoys"]["cogship-9"]["last_error"]


def test_a_second_scheduler_is_reported_once_an_hour(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(nw, "state_dir", lambda repo: tmp_path / "state")
    nw.report_lock_refusal(tmp_path, "another nightwatch is running (pid 1)")
    nw.report_lock_refusal(tmp_path, "another nightwatch is running (pid 1)")
    nw.report_lock_refusal(tmp_path, "another nightwatch is running (pid 1)")
    captured = capsys.readouterr()
    assert captured.err.count("another nightwatch") == 1


def test_policy_describe_names_the_effective_levers():
    line = _policy(max_workers=2, max_spawns_per_tick=1, max_spawns_per_hour=8).describe()
    assert "workers<=2" in line
    assert "spawns=1/tick,8/h" in line
    assert "enable=goad,coin,collect,dispatch" in line


# ---------------------------------------------------------------------------
# Charter lane — the pleasance pickup drives dispatch (no vigil file)
# ---------------------------------------------------------------------------

def test_charter_ready_quest_is_dispatched_fresh(tmp_path):
    snap = _snap(_q("Q234", "CHARTERED", worktree=""))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers(),
                         charter_ready=["Q234"])
    assert [(a.kind, a.quest_ids) for a in actions] == [("dispatch", ["Q234"])]
    assert actions[0].meta["fresh"] is True
    assert actions[0].meta["serf_index"] == 0
    assert "pickup lane" in actions[0].reason


def test_charter_ready_a_non_chartered_quest_is_ignored(tmp_path):
    snap = _snap(_q("Q234", "PLANNED", worktree=""))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers(),
                         charter_ready=["Q234"])
    assert actions == []


def test_charter_ready_skips_a_quest_that_already_has_a_worktree(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q234", "CHARTERED", wt))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers(),
                             charter_ready=["Q234"])
    assert actions == []
    assert any(n["level"] == "skip" and "worktree" in n["text"] for n in notes)


def test_charter_ready_respects_the_worker_ceiling(tmp_path):
    snap = _snap(_q("Q234", "CHARTERED", worktree=""))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path), _policy(max_workers=3),
                             _workers(count=3), charter_ready=["Q234"])
    assert actions == []
    assert any("ceiling" in n["text"] for n in notes)


def test_charter_ready_takes_one_capacity_slot(tmp_path):
    snap = _snap(_q("Q1", "CHARTERED", worktree=""), _q("Q2", "CHARTERED", worktree=""))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path),
                         _policy(max_spawns_per_tick=1, max_workers=4),
                         _workers(), charter_ready=["Q1", "Q2"])
    assert [(a.kind, a.quest_ids) for a in actions] == [("dispatch", ["Q1"])]


def test_charter_ready_a_dispatch_is_disabled_is_skipped(tmp_path):
    snap = _snap(_q("Q234", "CHARTERED", worktree=""))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path),
                             _policy(allow_dispatch=False), _workers(),
                             charter_ready=["Q234"])
    assert actions == []
    assert any(n["level"] == "skip" and "dispatch is disabled" in n["text"] for n in notes)


def test_charter_ready_honours_the_backoff_from_a_failed_dispatch(tmp_path):
    state = nw.load_state(tmp_path)
    state["quests"]["Q234"] = {"retry_at": time.time() + 3600}
    snap = _snap(_q("Q234", "CHARTERED", worktree=""))
    actions, _ = nw.plan(snap, state, _policy(), _workers(), charter_ready=["Q234"])
    assert actions == []


def test_failed_fresh_dispatch_sets_a_backoff_but_no_serf_index(tmp_path):
    state = nw.load_state(tmp_path)
    action = nw.Action(kind="dispatch", quest_ids=["Q234"],
                       meta={"fresh": True, "retry_backoff": 300})
    nw._apply_bookkeeping(state, action, {"ok": False, "stderr_tail": "boom"}, 1000.0)
    assert state["quests"]["Q234"]["retry_at"] == 1300.0
    # a successful fresh dispatch records nothing (the Quest moves to WORKING
    # upstream, so it never re-triggers); it must not advance a serf index that
    # does not exist
    nw._apply_bookkeeping(state, action, {"ok": True, "stderr_tail": ""}, 2000.0)
    assert "serf_index" not in state["quests"]["Q234"]
    assert state["quests"]["Q234"]["retry_at"] == 0


def test_fresh_dispatch_runs_with_standup(tmp_path, monkeypatch):
    from court.nightwatch import Action
    called = {}

    def fake_court(repo, *argv, timeout=None, env=None):
        called["argv"], called["env"] = argv, env

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(nw, "_court", fake_court)
    res = nw.execute(tmp_path, Action("dispatch", quest_ids=["Q7"], meta={"fresh": True}))
    assert res["ok"]
    assert list(called["argv"]) == ["dispatch", "Q7", "--standup"]
    assert called["env"].get("COURT_WATCH_DISPATCH") == "1"


def _charter_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".court" / "quests").mkdir(parents=True)
    return repo


def _charter_md(qid, status, prereqs=()):
    lines = ["---", f"id: {qid}", f"title: {qid}", f"status: {status}", "---", "",
             f"# {qid}", "", "## The Kingdom Requires", "x"]
    if prereqs:
        lines += ["", "## Prerequisites"] + [f"- {p}" for p in prereqs]
    return "\n".join(lines) + "\n"


def test_charter_ready_empty_prereqs_are_satisfied(tmp_path):
    repo = _charter_repo(tmp_path)
    (repo / ".court" / "quests" / "Q234-New.md").write_text(
        _charter_md("Q234-New", "CHARTERED"), encoding="utf-8")
    qmap = {"Q234-New": {"id": "Q234-New", "status": "CHARTERED", "worktree": ""}}
    assert nw._charter_ready(repo, qmap) == ["Q234-New"]


def test_charter_ready_requires_every_prereq_to_hold(tmp_path):
    repo = _charter_repo(tmp_path)
    (repo / ".court" / "quests" / "Q234-New.md").write_text(
        _charter_md("Q234-New", "CHARTERED", prereqs=["quest-status:Q999=GATE"]),
        encoding="utf-8")
    qmap = {"Q234-New": {"id": "Q234-New", "status": "CHARTERED", "worktree": ""}}
    assert nw._charter_ready(repo, qmap) == []
    # the condition names a short-id prefix; once the dependency reaches GATE
    # the quest becomes dispatch-ready
    qmap["Q999-Old"] = {"id": "Q999-Old", "status": "GATE", "worktree": None}
    assert nw._charter_ready(repo, qmap) == ["Q234-New"]


def test_charter_ready_skips_working_occupied_broken(tmp_path):
    repo = _charter_repo(tmp_path)
    (repo / ".court" / "quests" / "Q5-Char.md").write_text(
        _charter_md("Q5-Char", "CHARTERED"), encoding="utf-8")
    (repo / ".court" / "quests" / "Q6-Work.md").write_text(
        _charter_md("Q6-Work", "WORKING"), encoding="utf-8")
    (repo / ".court" / "quests" / "Q7-Broken.md").write_text(
        "garbage, not a charter", encoding="utf-8")
    qmap = {
        # has a worktree -> not eligible
        "Q5-Char": {"id": "Q5-Char", "status": "CHARTERED", "worktree": "/wt"},
        # not CHARTERED
        "Q6-Work": {"id": "Q6-Work", "status": "WORKING", "worktree": ""},
        # unparseable -> conservatively not ready (pickup would have rejected it)
        "Q7-Broken": {"id": "Q7-Broken", "status": "CHARTERED", "worktree": ""},
    }
    assert nw._charter_ready(repo, qmap) == []


def test_tick_folds_branch_paperwork_before_planning(tmp_path, monkeypatch):
    folded = []
    monkeypatch.setattr(nw, "fold_ledger", lambda repo, say=print: folded.append(Path(repo)) or True)
    monkeypatch.setattr(nw, "snapshot", lambda repo, timeout=None: {"quests": []})
    nw.tick(tmp_path, _policy(), nw.load_state(tmp_path), say=lambda *a, **k: None)
    assert folded == [tmp_path]


def test_tick_dry_run_does_not_fold_the_ledger(tmp_path, monkeypatch):
    folded = []
    monkeypatch.setattr(nw, "fold_ledger", lambda repo, say=print: folded.append(Path(repo)) or True)
    monkeypatch.setattr(nw, "snapshot", lambda repo, timeout=None: {"quests": []})
    nw.tick(tmp_path, _policy(), nw.load_state(tmp_path), dry_run=True, say=lambda *a, **k: None)
    assert folded == []


# ---------------------------------------------------------------------------
# Drift: a mechanical rebase precedes the spawn
# ---------------------------------------------------------------------------

def test_a_drifted_tribute_ready_quest_rebases_instead_of_coining(tmp_path):
    """A Quest that is merely behind castle is not broken: rebase it this tick,
    then coin it next tick once the branch is clean."""
    wt = _free(tmp_path)
    state = nw.load_state(tmp_path)
    snap = _snap(_q("Q1", "TRIBUTE_READY", wt, git_status={"behind": 7}))
    actions, _ = nw.plan(snap, state, _policy(max_workers=4), _workers())
    (action,) = actions
    assert (action.kind, action.quest_ids) == ("rebase", ["Q1"])
    assert "7 commit(s)" in action.reason

    nw._apply_bookkeeping(
        state, action, {"ok": True, "stdout_tail": "🔄 Q1 merged: 7 -> 0 behind."}, 1000.0)
    assert state["quests"]["Q1"]["last_rebase_ts"] == 1000.0
    assert not state["quests"]["Q1"].get("coins")

    clean = _snap(_q("Q1", "TRIBUTE_READY", wt, git_status={"behind": 0}))
    actions, _ = nw.plan(clean, state, _policy(max_workers=4), _workers(), now=1301.0)
    assert [a.kind for a in actions] == ["coin"]


def test_bookkeeping_only_drift_does_not_rebase(tmp_path):
    """The trunk gains a `.court/board.json` snapshot every tick, so a branch
    is always "1 behind". Rebasing on that churn re-merges the same branch
    every tick and burns the spawn budget, so bookkeeping-only drift is not
    a rebase trigger."""
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "TRIBUTE_READY", wt,
                    git_status={"behind": 1, "behind_code": 0}))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path), _policy(max_workers=4), _workers())
    assert [a.kind for a in actions] == ["coin"]
    assert any("bookkeeping-only" in n["text"] for n in notes)


def test_scaffolding_only_dirt_does_not_block_a_rebase(tmp_path):
    """`.kilo/TASK*.md` prompts are regenerated on every spawn; they must not
    freeze a drifted Quest out of convergence forever."""
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "TRIBUTE_READY", wt,
                    git_status={"behind": 5, "behind_code": 5,
                                "dirty": True, "dirty_disposable_only": True}))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(max_workers=4), _workers())
    assert [(a.kind, a.quest_ids) for a in actions] == [("rebase", ["Q1"])]


def test_real_dirt_still_blocks_a_rebase(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "TRIBUTE_READY", wt,
                    git_status={"behind": 5, "behind_code": 5, "dirty": True}))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path), _policy(max_workers=4), _workers())
    assert "rebase" not in [a.kind for a in actions]
    assert any("DIRTY" in n["text"] for n in notes)


def test_a_conflicted_rebase_holds_the_quest_and_does_not_coin(tmp_path):
    wt = _free(tmp_path)
    state = nw.load_state(tmp_path)
    action = nw.Action(kind="rebase", quest_ids=["Q1"], meta={"retry_backoff": 300})
    nw._apply_bookkeeping(state, action, {
        "ok": False,
        "stdout_tail": ("🔴 Q1 CONFLICT in [.court/quests/Q1-X.md, apps/x.py] — "
                        "aborted, needs manual resolution."),
    }, 1000.0)
    mc = state["quests"]["Q1"]["merge_conflict"]
    assert mc["files"] == [".court/quests/Q1-X.md", "apps/x.py"]
    assert not state["quests"]["Q1"].get("coins")

    snap = _snap(_q("Q1", "TRIBUTE_READY", wt, git_status={"behind": 7}))
    actions, notes = nw.plan(snap, state, _policy(max_workers=4), _workers(), now=1100.0)
    assert actions == []
    assert any(n.get("key") == "merge_conflict" for n in notes)


def test_execute_treats_a_reported_rebase_conflict_as_a_failure(tmp_path, monkeypatch):
    class R:
        returncode = 0
        stdout = "🔴 Q1 CONFLICT in [a.md] — aborted, needs manual resolution."
        stderr = ""

    monkeypatch.setattr(nw, "_court", lambda *a, **k: R())
    res = nw.execute(tmp_path, nw.Action(kind="rebase", quest_ids=["Q1"]))
    assert res["ok"] is False
    assert res["argv"] == ["rebase", "Q1"]
    assert nw._merge_conflict_files(res) == ["a.md"]


def test_rebase_disabled_yields_no_rebase_action(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "TRIBUTE_READY", wt, git_status={"behind": 7}))
    pol = _policy(enabled=("goad", "coin", "collect", "dispatch", "teardown"))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), pol, _workers())
    assert [a.kind for a in actions] == ["coin"]


def test_ready_to_raze_is_razed_by_default(tmp_path):
    """Razing is mechanical now: a READY_TO_RAZE Quest with a free worktree is
    planned for `court raze` (which verifies promotion, then tears down)."""
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "READY_TO_RAZE", wt))
    actions, notes = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers(),
                             raze_regate=[])
    assert [a.kind for a in actions] == ["raze"]
    assert not [n for n in notes if n["level"] == "human"]


def test_already_razed_quest_still_gets_its_teardown(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "READY_TO_RAZE", wt, razed_at="2026-11-08T00:00:00Z"))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers(),
                         raze_regate=[])
    assert [a.kind for a in actions] == ["raze"]
    assert "teardown" in actions[0].reason


def test_a_raze_the_gate_refuses_is_regated_mechanically(tmp_path):
    """The branch tip not being an ancestor of castle is a mechanical problem,
    not a human one: hand the Quest back to a fresh convoy."""
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "READY_TO_RAZE", wt))
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers(),
                         raze_regate=["Q1"])
    assert [a.kind for a in actions] == ["regate"]


def test_regates_are_batched_by_app(tmp_path):
    """Same-app raze refusals share one fresh convoy (and one unified suite)
    instead of minting a singleton convoy per Quest."""
    wts = [_free(tmp_path, n) for n in ("a", "b", "c")]
    snap = _snap(
        _q("Q1", "READY_TO_RAZE", wts[0], app="inventory"),
        _q("Q2", "READY_TO_RAZE", wts[1], app="inventory"),
        _q("Q3", "READY_TO_RAZE", wts[2], app="marketing"),
    )
    actions, _ = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers(),
                         raze_regate=["Q1", "Q2", "Q3"])
    regates = [a for a in actions if a.kind == "regate"]
    assert sorted(tuple(a.quest_ids) for a in regates) == [("Q1", "Q2"), ("Q3",)]


def test_regate_batches_respect_the_size_cap(tmp_path):
    wts = [_free(tmp_path, n) for n in ("a", "b", "c")]
    snap = _snap(*[_q(qid, "READY_TO_RAZE", wts[i], app="crm")
                   for i, qid in enumerate(("Q1", "Q2", "Q3"))])
    actions, notes = nw.plan(snap, nw.load_state(tmp_path),
                             _policy(regate_batch_max=2), _workers(),
                             raze_regate=["Q1", "Q2", "Q3"])
    regates = [tuple(a.quest_ids) for a in actions if a.kind == "regate"]
    assert regates == [("Q1", "Q2")]
    assert any(n.get("key") == "regate_remainder:crm" for n in notes)


def test_only_one_multi_quest_gatehouse_convoy_is_planned_per_tick(tmp_path):
    wts = [_free(tmp_path, str(i)) for i in range(4)]
    snap = _snap(
        _q("Q1", "READY_TO_RAZE", wts[0], app="inventory"),
        _q("Q2", "READY_TO_RAZE", wts[1], app="inventory"),
        _q("Q3", "READY_TO_RAZE", wts[2], app="marketing"),
        _q("Q4", "READY_TO_RAZE", wts[3], app="marketing"),
    )
    actions, notes = nw.plan(snap, nw.load_state(tmp_path), _policy(), _workers(),
                             raze_regate=["Q1", "Q2", "Q3", "Q4"])
    regates = [tuple(a.quest_ids) for a in actions if a.kind == "regate"]
    assert regates == [("Q1", "Q2")]
    assert any(n.get("key") == "regate_gatehouse_busy:marketing" for n in notes)


def test_a_batched_regate_passes_one_comma_separated_positional(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        nw, "_court",
        lambda repo, *argv, **kw: seen.setdefault("argv", list(argv))
        or __import__("subprocess").CompletedProcess(argv, 1, "", ""))
    nw.execute(tmp_path, nw.Action(kind="regate", quest_ids=["Q1", "Q2"]))
    assert seen["argv"] == ["collect", "Q1,Q2", "--regate", "--standup"]




def test_a_regate_that_keeps_failing_escalates_to_a_steward(tmp_path):
    wt = _free(tmp_path)
    state = nw.load_state(tmp_path)
    state["quests"] = {"Q1": {"regate_fails": 2}}
    snap = _snap(_q("Q1", "READY_TO_RAZE", wt))
    actions, notes = nw.plan(snap, state, _policy(max_regates=2), _workers(),
                             raze_regate=["Q1"])
    assert actions == []
    assert any(n["level"] == "human" and n.get("key") == "regate_exhausted" for n in notes)


def test_a_failed_regate_counts_and_a_success_clears_it(tmp_path):
    state = nw.load_state(tmp_path)
    action = nw.Action(kind="regate", quest_ids=["Q1"], meta={"retry_backoff": 300})
    nw._apply_bookkeeping(state, action, {"ok": False, "stderr_tail": "boom"}, 1000.0)
    assert state["quests"]["Q1"]["regate_fails"] == 1
    nw._apply_bookkeeping(state, action, {"ok": True, "stdout_tail": ""}, 2000.0)
    assert state["quests"]["Q1"]["regate_fails"] == 0


def test_pytest_db_reaping_runs_only_on_an_idle_board_once_per_interval(tmp_path):
    wt = _free(tmp_path)
    snap = _snap(_q("Q1", "WORKING", wt, worker="live"))
    state = nw.load_state(tmp_path)
    state["supervisor_started"] = 1.0
    pol = _policy(pg_prune_interval=3600)

    actions, _ = nw.plan(snap, state, pol, _workers(0), now=100_000.0)
    assert [a.kind for a in actions] == ["pgprune"]

    # A live worker makes the board non-idle, and the interval is respected.
    actions, _ = nw.plan(snap, state, pol, _workers(1), now=100_000.0)
    assert not [a for a in actions if a.kind == "pgprune"]
    state["maintenance"] = {"pg_prune_ts": 100_000.0}
    actions, _ = nw.plan(snap, state, pol, _workers(0), now=100_060.0)
    assert not [a for a in actions if a.kind == "pgprune"]
    actions, _ = nw.plan(snap, state, pol, _workers(0), now=103_601.0)
    assert [a.kind for a in actions if a.kind == "pgprune"]


def test_an_open_charter_request_summons_one_steward_turn(tmp_path):
    """Requests-in, charters-out: the watch charters a request instead of
    letting the requester invent a Quest number."""
    state = nw.load_state(tmp_path)
    snap = _snap()
    req = {"id": "req-2026-11-09-thing", "title": "Ship the thing"}
    actions, _ = nw.plan(snap, state, _policy(), _workers(), now=1000.0,
                         charter_requests=[req])
    assert [(a.kind, a.quest_ids) for a in actions] == [("charter", ["req-2026-11-09-thing"])]


def test_chartering_honours_capacity_and_the_cooldown(tmp_path):
    state = nw.load_state(tmp_path)
    snap = _snap()
    req = {"id": "req-1", "title": "Ship it"}
    actions, _ = nw.plan(snap, state, _policy(max_workers=1), _workers(count=1), now=1000.0,
                         charter_requests=[req])
    assert not [a for a in actions if a.kind == "charter"]

    pol = _policy()
    actions, _ = nw.plan(snap, state, pol, _workers(), now=1000.0, charter_requests=[req])
    assert [a.kind for a in actions] == ["charter"]
    nw._apply_bookkeeping(state, actions[0], {"ok": False, "stderr_tail": "boom"}, 1000.0)
    actions, _ = nw.plan(snap, state, pol, _workers(), now=1000.0 + 60,
                         charter_requests=[req])
    assert not [a for a in actions if a.kind == "charter"]


def test_a_charter_request_that_keeps_failing_escalates_to_a_human(tmp_path):
    state = nw.load_state(tmp_path)
    state["requests"] = {"req-1": {"fails": 2}}
    snap = _snap()
    actions, notes = nw.plan(snap, state, _policy(max_charter_fails=2), _workers(), now=1000.0,
                             charter_requests=[{"id": "req-1", "title": "Ship it"}])
    assert not [a for a in actions if a.kind == "charter"]
    assert any(n["level"] == "human" and n.get("key") == "charter_exhausted" for n in notes)


def test_chartering_is_disabled_without_the_verb(tmp_path):
    actions, notes = nw.plan(_snap(), nw.load_state(tmp_path), _policy(enabled=("goad",)),
                             _workers(), charter_requests=[{"id": "req-1", "title": "Ship it"}])
    assert not [a for a in actions if a.kind == "charter"]
    assert any("chartering disabled" in n["text"] for n in notes)
