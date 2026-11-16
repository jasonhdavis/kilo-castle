"""Collector re-applies `collect_min_batch` after pre-pack convergence (2026-11-16).

`court collect` graduates to the pack step only after converging each candidate
onto `castle`. That gate can still thin a cohort the planner already accepted
(a castle-side commit landed since the planner's probe — the cogship-618 class),
and because `collect_min_batch` was a planner-only policy the survivors then
packed SHORT (1-2 Quests). The watch now passes the cohort floor it planned, and
the collector defers the tick (short retry) instead of stamping a degraded convoy.
"""
import io
import subprocess
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from court import cli, nightwatch as nw, store
from court.models import Quest


# ---------------------------------------------------------------------------
# Planner -> execute: the cohort floor rides on the collect action
# ---------------------------------------------------------------------------

def _capture_argv(monkeypatch, tmp_path, action):
    seen = {}
    monkeypatch.setattr(
        nw, "_court",
        lambda repo, *argv, **kw: seen.setdefault("argv", list(argv))
        or subprocess.CompletedProcess(argv, 0, "", ""))
    nw.execute(tmp_path, action)
    return seen["argv"]


def test_execute_passes_the_collect_floor(monkeypatch, tmp_path):
    argv = _capture_argv(monkeypatch, tmp_path, nw.Action(
        kind="collect", quest_ids=["Q1", "Q2", "Q3"], meta={"collect_min_batch": 3}))
    assert argv == ["collect", "Q1,Q2,Q3", "--collect-min-batch", "3", "--standup"]


def test_execute_omits_the_flag_below_two(monkeypatch, tmp_path):
    for floor in (0, 1):
        argv = _capture_argv(monkeypatch, tmp_path, nw.Action(
            kind="collect", quest_ids=["Q7"], meta={"collect_min_batch": floor}))
        assert argv == ["collect", "Q7", "--standup"], floor


def _q(qid, status, worktree="/tmp/nw-cmb-wt", **kw):
    base = {"id": qid, "kind": "quest", "status": status, "worktree": worktree,
            "worker": {"state": "idle"}, "gatekeeper_session_id": "",
            "cogship_id": "", "pending_audience": False}
    base.update(kw)
    return base


def _free(tmp_path, name):
    p = tmp_path / name
    p.mkdir(exist_ok=True)
    return str(p)


def _workers():
    return {"count": 0, "rss_mb": 0.0, "by_role": {}, "workers": [], "dirs": {}}


def test_plan_clamps_the_floor_to_the_emitted_batch(tmp_path):
    """`collect_batch_max` can be below `collect_min_batch`; the floor must be
    clamped to the batch actually emitted, or every collect would defer forever."""
    wts = [_free(tmp_path, f"w{i}") for i in range(6)]
    snap = {"quests": [_q(f"Q{i}", "GATE", wts[i]) for i in range(6)]}
    actions, _ = nw.plan(snap, nw.load_state(tmp_path),
                         nw.Policy(max_workers=5, max_spawns_per_tick=10,
                                   max_spawns_per_hour=20, collect_min_batch=5,
                                   collect_batch_max=3),
                         _workers(), now=1000.0)
    collect = [a for a in actions if a.kind == "collect"]
    assert len(collect) == 1
    assert len(collect[0].quest_ids) == 3
    assert collect[0].meta["collect_min_batch"] == 3


def test_a_deferred_collect_retries_next_tick_not_the_noop_window(tmp_path):
    state = nw.load_state(tmp_path)
    action = nw.Action(kind="collect", quest_ids=["Q1", "Q2"], cogship="",
                       meta={"convoy": "new", "retry_backoff": 300, "noop_backoff": 3600})
    nw._apply_bookkeeping(state, action,
                          {"ok": True, "packed": False, "deferred": True}, 1000.0)
    assert state["convoys"]["new"]["retry_at"] == 1300.0  # one tick, not +3600
    # ... while a plain no-op still backs off hard.
    nw._apply_bookkeeping(state, action, {"ok": True, "packed": False}, 1000.0)
    assert state["convoys"]["new"]["retry_at"] == 4600.0


# ---------------------------------------------------------------------------
# cmd_collect: defer when convergence thins the cohort below the floor
# ---------------------------------------------------------------------------

def _quest(qid, worktree):
    return Quest(
        id=qid, title="t", app="orders", concern="c", status="TRIBUTE_READY",
        branch=f"quest/{qid.lower()}", cogship_id="", worktree=worktree,
        body_sections={
            "The Kingdom Requires": "x",
            "Expected Tribute": "- [x] done",
            "Master of Coin's Audit": "- **Verdict:** PASS\n- **UI Review:** None required",
        },
    )


def _args(**overrides):
    args = type("Args", (), {})()
    args.base = "castle"
    args.no_commit = True
    args.force = False
    args.skip_ui_review = False
    args.hotfix = False
    args.regate = False
    args.no_premerge = False
    args.cogship = None
    args.standup = False
    args.collect_min_batch = 0
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


class _CleanAudit:
    git_status = {"dirty": False}
    violations: list = []


def _run(quests, args, tmp_path, monkeypatch, conv):
    """Drive cmd_collect to (at least) the cohort-floor point. `require_op_lock`
    raises a sentinel so a suite that DID proceed past the deferral is obvious."""
    def _lock(name):
        raise RuntimeError("reached-op-lock")
    monkeypatch.setattr(cli, "resolve_quest_selection", lambda *a, **k: list(quests))
    monkeypatch.setattr(cli.git_ops, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "require_op_lock", _lock)
    monkeypatch.setattr(cli.ward, "audit_quest", lambda q, base_branch="castle": _CleanAudit())
    monkeypatch.setattr(cli.migration_guard, "scan_branch_contraband", lambda *a, **k: [])
    monkeypatch.setattr(cli.convoy_guard, "studio_pending", lambda root, qid: (False, ""))
    monkeypatch.setattr(cli.convoy_guard, "ui_mandate_outstanding", lambda root, qid: (False, ""))
    monkeypatch.setattr(cli.atelier, "member", lambda root, qid: None)
    monkeypatch.setattr(cli.git_ops, "rebase_worktree_onto_base",
                        lambda wt, base_branch="castle", own_quest_id=None:
                        conv.get(own_quest_id, {"ok": True, "merged": False,
                                                "already_current": True}))
    monkeypatch.setattr(store, "save_many", lambda quests, *a, **k: None)
    (tmp_path / ".kilo" / "worktrees").mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    with patch.object(cli.store, "save"):
        with redirect_stdout(buf):
            cli.cmd_collect(args)
    return buf.getvalue()


def test_cmd_collect_defers_when_convergence_thins_the_cohort(tmp_path, monkeypatch):
    w1, w2 = _free(tmp_path, "wt1"), _free(tmp_path, "wt2")
    q1, q2 = _quest("Q1-A", w1), _quest("Q2-B", w2)
    conv = {"Q1-A": {"ok": True, "merged": True, "before_behind": 3},
            "Q2-B": {"ok": True, "conflict": True, "conflicting_files": ["apps/orders/models.py"]}}
    out = _run([q1, q2], _args(collect_min_batch=2), tmp_path, monkeypatch, conv)
    assert "Deferred:" in out
    assert "Stamped" not in out


def test_cmd_collect_does_not_defer_without_the_flag(tmp_path, monkeypatch):
    w1, w2 = _free(tmp_path, "wt1"), _free(tmp_path, "wt2")
    q1, q2 = _quest("Q1-A", w1), _quest("Q2-B", w2)
    conv = {"Q1-A": {"ok": True, "merged": True, "before_behind": 3},
            "Q2-B": {"ok": True, "conflict": True, "conflicting_files": ["apps/orders/models.py"]}}
    # No --collect-min-batch (default 0): the survivor proceeds past the deferral.
    with pytest.raises(RuntimeError, match="reached-op-lock"):
        _run([q1, q2], _args(collect_min_batch=0), tmp_path, monkeypatch, conv)


def test_cmd_collect_force_bypasses_the_floor(tmp_path, monkeypatch):
    w1, w2 = _free(tmp_path, "wt1"), _free(tmp_path, "wt2")
    q1, q2 = _quest("Q1-A", w1), _quest("Q2-B", w2)
    conv = {"Q1-A": {"ok": True, "merged": True},
            "Q2-B": {"ok": True, "conflict": True, "conflicting_files": ["x.py"]}}
    with pytest.raises(RuntimeError, match="reached-op-lock"):
        _run([q1, q2], _args(collect_min_batch=2, force=True), tmp_path, monkeypatch, conv)
