"""Tests for the two clog-handling primitives on the Steward→watch hand-off:

* `rebase:<QID>` as a NON-spawning vigil verb (mechanical convergence first,
  worker second), and
* `--brief` on `dispatch:`/`goad:` vigils, so a queued hand-off carries the
  Steward's actual remediation instruction instead of re-stating a charter the
  worker already satisfied.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from court import cli, nightwatch as nw, vigils


def _tmp_repo(tmp_path):
    (tmp_path / ".court").mkdir(exist_ok=True)
    return tmp_path


# ---------------------------------------------------------------------------
# rebase as a vigil verb
# ---------------------------------------------------------------------------


def test_rebase_is_a_non_spawning_vigil_verb():
    spec = vigils.build(
        id="rebase-q234",
        when=["quest-status:Q234=TRIBUTE_READY"],
        then="rebase:Q234",
        reason="converge the drifted worktree before the audit spawn",
        expires="6h",
    )
    argv, spawns = vigils.action_argv(spec["then"], spec)
    assert argv == ["rebase", "Q234"]
    assert spawns is False
    assert "rebase" not in vigils.SPAWNING_ACTIONS


def test_bare_rebase_without_a_quest_is_rejected():
    with pytest.raises(vigils.VigilError):
        vigils.build(
            id="rebase-nothing",
            when=["after:2030-01-01T00:00:00Z"],
            then="rebase",
            reason="a rebase needs a quest to converge",
            expires="6h",
        )


# ---------------------------------------------------------------------------
# brief plumbing
# ---------------------------------------------------------------------------


def test_brief_rides_a_dispatch_vigil_into_the_argv():
    spec = vigils.build(
        id="dispatch-q234",
        when=["quest-status:Q234=GATE"],
        then="dispatch:Q234",
        reason="remediate the shared docs conflict with a union resolution",
        expires="6h",
        brief="/repo/.court/briefs/Q234.md",
    )
    argv, spawns = vigils.action_argv(spec["then"], spec)
    assert argv == ["dispatch", "Q234", "--standup", "--brief", "/repo/.court/briefs/Q234.md"]
    assert spawns is True

    # without the spec the generic path stays prompt-free (pure grammar)
    assert vigils.action_argv(spec["then"])[0] == ["dispatch", "Q234", "--standup"]


def test_brief_is_rejected_on_actions_with_no_prompt_to_override():
    with pytest.raises(vigils.VigilError):
        vigils.build(
            id="collect-q234",
            when=["quest-status:Q234=TRIBUTE_READY"],
            then="collect:Q234",
            reason="pack the tribute once it is rendered and rebased",
            expires="6h",
            brief="/repo/.court/briefs/Q234.md",
        )


def test_resolve_brief_requires_a_repo_local_file(tmp_path):
    repo = _tmp_repo(tmp_path)
    repo = repo.resolve()
    assert cli._resolve_vigil_brief(repo, "") == ""

    brief = repo / ".court" / "briefs" / "Q1.md"
    brief.parent.mkdir(parents=True)
    brief.write_text("resolve the union by keeping both blocks\n", encoding="utf-8")
    assert cli._resolve_vigil_brief(repo, ".court/briefs/Q1.md") == str(brief.resolve())

    with pytest.raises(ValueError):
        cli._resolve_vigil_brief(repo, ".court/briefs/missing.md")

    outside = tmp_path.parent / "outside-brief.md"
    outside.write_text("nope\n", encoding="utf-8")
    with pytest.raises(ValueError):
        cli._resolve_vigil_brief(repo, str(outside))

    big = repo / ".court" / "briefs" / "big.md"
    big.write_text("x" * (cli.BRIEF_MAX_BYTES + 1), encoding="utf-8")
    with pytest.raises(ValueError):
        cli._resolve_vigil_brief(repo, str(big))


# ---------------------------------------------------------------------------
# Night's Watch planning / bookkeeping
# ---------------------------------------------------------------------------


def _quest(qid, status, worktree="/tmp/nw-wt", **kw):
    base = {
        "id": qid,
        "kind": "quest",
        "status": status,
        "worktree": worktree,
        "worker": {"state": "idle"},
        "git_status": {"behind": 0, "behind_code": 0, "dirty": False},
        "pending_audience": False,
    }
    base.update(kw)
    return base


def _plan(tmp_path, snap, max_workers=3, vigils_ready=None):
    policy = nw.Policy(max_workers=max_workers, max_spawns_per_tick=5,
                       max_spawns_per_hour=20)
    return nw.plan(snap, nw.load_state(tmp_path), policy,
                   {"count": 0, "rss_mb": 0.0, "by_role": {}, "workers": [], "dirs": {}},
                   ready_vigils=vigils_ready or [])


def test_queued_rebase_does_not_need_worker_capacity(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    wt.mkdir()
    spec = vigils.build(
        id="rebase-q1",
        when=["quest-status:Q1=TRIBUTE_READY"],
        then="rebase:Q1",
        reason="converge before the audit so the coin never starts stale",
        expires="6h",
    )
    vigils.save(tmp_path, spec)
    monkeypatch.setattr(nw, "snapshot", lambda repo: {"quests": [_quest("Q1", "TRIBUTE_READY", str(wt))]})

    actions, _notes = _plan(tmp_path, {"quests": [_quest("Q1", "TRIBUTE_READY", str(wt))]},
                            max_workers=0, vigils_ready=[(spec, "test")])
    rebase = [a for a in actions if a.kind == "vigil"]
    assert [a.meta["then"] for a in rebase] == ["rebase:Q1"]
    assert rebase[0].meta["spawns"] is False
    assert rebase[0].quest_ids == ["Q1"]


def test_brief_is_threaded_into_the_planned_action(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    wt.mkdir()
    spec = vigils.build(
        id="goad-q1",
        when=["quest-status:Q1=WORKING"],
        then="goad:Q1",
        reason="remediation: resolve the shared agent-api docs union",
        expires="6h",
        brief="/repo/.court/briefs/Q1.md",
    )
    vigils.save(tmp_path, spec)
    actions, _notes = _plan(tmp_path, {"quests": [_quest("Q1", "WORKING", str(wt))]},
                            vigils_ready=[(spec, "test")])
    goad = [a for a in actions if a.kind == "vigil"][0]
    assert goad.meta["brief"] == "/repo/.court/briefs/Q1.md"


def test_vigil_merge_conflict_is_recorded_as_a_hold(tmp_path):
    """A queued rebase/coin that dies on `git merge castle` must stamp the same
    hold the heuristic paths use, otherwise a chained vigil fires into the
    identical conflict next tick and burns its own queue entry."""
    spec = vigils.build(
        id="rebase-q1",
        when=["after:2030-01-01T00:00:00Z"],
        then="rebase:Q1",
        reason="converge the worktree before the audit worker starts",
        expires="6h",
    )
    vigils.save(tmp_path, spec)
    action = nw.Action(kind="vigil", quest_ids=["Q1"], reason="test",
                       meta={"vigil_id": "rebase-q1", "then": "rebase:Q1", "spawns": False})
    state = {}
    nw._apply_bookkeeping(state, action, {
        "ok": False,
        "exit": 0,
        "stdout_tail": "🔴 Q1 CONFLICT in [apps/foo/models.py] — aborted",
        "stderr_tail": "",
    }, now=1000.0, repo=tmp_path)
    hold = state["quests"]["Q1"]["merge_conflict"]
    assert hold["files"] == ["apps/foo/models.py"]
    # the vigil is retired, not left active to re-fire
    assert vigils.load_all(tmp_path) == []


# ---------------------------------------------------------------------------
# court enqueue derives the action from the stuck status
# ---------------------------------------------------------------------------


def _write_quest(repo: Path, qid: str, status: str) -> Path:
    p = repo / ".court" / "quests" / f"{qid}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\n"
        f"id: {qid}\n"
        f"title: {qid} fixture\n"
        "kind: quest\n"
        "app: castle\n"
        "section: Feature\n"
        f"status: {status}\n"
        f"branch: quest/{qid.lower()}\n"
        "worktree: \n"
        "---\n\n"
        "# " + qid + " fixture\n\n## The Kingdom Requires\n\nfixture\n",
        encoding="utf-8",
    )
    return p


@pytest.mark.parametrize("status,action", [
    ("PLANNED", "dispatch"),
    ("CHARTERED", "dispatch"),
    ("WORKING", "goad"),
    ("TRIBUTE_READY", "coin"),
    ("GATE", "collect"),
])
def test_enqueue_derives_the_action_from_status(tmp_path, monkeypatch, status, action):
    repo = _tmp_repo(tmp_path)
    quest_file = _write_quest(repo, "Q900-Test", status)
    assert quest_file.is_file()
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli.git_ops, "get_repo_root", lambda: repo)

    args = SimpleNamespace(quest="Q900-Test", why="fixture hand-off for the test",
                           action=None, brief=None, expires="6h", after="0s",
                           repo=str(repo))
    cli.cmd_enqueue(args)

    specs = vigils.load_all(repo)
    assert len(specs) == 1
    assert specs[0]["then"] == f"{action}:Q900-Test"
    assert specs[0]["max_fires"] == 1


def test_enqueue_rejects_an_action_that_cannot_advance_the_status(tmp_path, monkeypatch):
    repo = _tmp_repo(tmp_path)
    _write_quest(repo, "Q901-Test", "TRIBUTE_READY")
    monkeypatch.setattr(cli.git_ops, "get_repo_root", lambda: repo)
    args = SimpleNamespace(quest="Q901-Test", why="fixture hand-off for the test",
                           action="goad", brief=None, expires="6h", after="0s",
                           repo=str(repo))
    with pytest.raises(SystemExit):
        cli.cmd_enqueue(args)
    assert vigils.load_all(repo) == []
