"""Section-aware merge of a Quest's OWN charter during `git merge castle`.

The rebase helper auto-resolves foreign ledger files and scaffolding. A Quest's
own charter used to be the one paperwork file that always aborted: the branch
fills `## Tribute Rendered` and ticks Expected-Tribute boxes while castle keeps
updating the same file. These tests pin the deterministic policy: castle owns
frontmatter and every other section; the branch's tribute survives; checkbox
state is unioned; Expected-Tribute ITEM TEXT must match exactly.
"""
import json
import subprocess

from court import git_ops

CHARTER_ID = "Q020-Test"
CHARTER_PATH = ".court/quests/Q020-Test.md"
EVENTS_PATH = ".court/quests/Q020-Test.events.jsonl"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _charter(status="PLANNED", items=("First deliverable", "Second deliverable"),
             checked=(), tribute="", include_tribute=True, extras=()):
    lines = [
        "---",
        f"id: {CHARTER_ID}",
        "title: Own charter merge probe",
        "kind: quest",
        f"status: {status}",
        "updated_at: 2026-01-01T00:00:00Z",
        "---",
        "",
        f"# {CHARTER_ID} — Own charter merge probe",
        "",
        "## Castle Ledger",
        "",
        "- **2026-01-01T00:00:00Z** — OPEN → PLANNED: Quest created",
        "",
    ]
    for heading, body in extras:
        lines += [f"## {heading}", ""]
        if body:
            lines += [body, ""]
    lines += ["## Expected Tribute", ""]
    for i, item in enumerate(items, 1):
        mark = "x" if i in checked else " "
        lines.append(f"- [{mark}] {item}")
    if include_tribute:
        lines += ["", "## Tribute Rendered", ""]
        if tribute:
            lines += [tribute, ""]
    lines += ["## Master of Coin's Audit", ""]
    return "\n".join(lines)


def _events(*values):
    return "".join(
        json.dumps({"target": "field:status", "ts": ts, "value": value}) + "\n"
        for ts, value in values
    )


def _conflicted_repo(tmp_path, castle_items=None, castle_checked=(),
                     branch_extra=(), castle_extra=(),
                     branch_has_tribute=True, castle_tribute=""):
    """castle and a quest branch both edit Q020's own paperwork, then merge."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "castle")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".court" / "quests").mkdir(parents=True)
    (repo / CHARTER_PATH).write_text(_charter(), encoding="utf-8")
    (repo / EVENTS_PATH).write_text(
        _events(("2026-01-01T00:00:00Z", "PLANNED")), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")

    _git(repo, "checkout", "-q", "-b", "quest/q020-own-charter-merge")
    (repo / CHARTER_PATH).write_text(
        _charter(status="WORKING", checked=(1,),
                 tribute="## Ballad\n\nThe branch's rendered tribute.",
                 include_tribute=branch_has_tribute, extras=branch_extra),
        encoding="utf-8")
    (repo / EVENTS_PATH).write_text(
        _events(("2026-01-01T00:00:00Z", "PLANNED"), ("2026-01-02T00:00:00Z", "WORKING")),
        encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "branch tribute")

    _git(repo, "checkout", "-q", "castle")
    items = castle_items or ("First deliverable", "Second deliverable")
    (repo / CHARTER_PATH).write_text(
        _charter(status="TRIBUTE_READY", items=items, checked=castle_checked,
                 tribute=castle_tribute, extras=castle_extra),
        encoding="utf-8")
    (repo / EVENTS_PATH).write_text(
        _events(("2026-01-01T00:00:00Z", "PLANNED"), ("2026-01-03T00:00:00Z", "TRIBUTE_READY")),
        encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "castle advance")

    _git(repo, "checkout", "-q", "quest/q020-own-charter-merge")
    return repo


def test_own_charter_merge_keeps_branch_tribute_and_unions_toggles(tmp_path):
    repo = _conflicted_repo(tmp_path, castle_checked=(2,))
    res = git_ops.rebase_worktree_onto_base(repo, base_branch="castle", own_quest_id=CHARTER_ID)

    assert res["ok"] is True and res["merged"] is True, res
    assert f"{CHARTER_PATH} (own charter)" in res["auto_resolved_files"]
    merged = (repo / CHARTER_PATH).read_text(encoding="utf-8")
    # castle owns the frontmatter and the non-tribute sections
    assert "status: TRIBUTE_READY" in merged
    assert "## Castle Ledger" in merged
    # the branch's rendered tribute survives
    assert "The branch's rendered tribute." in merged
    # checkbox state is the union of both sides
    assert "- [x] First deliverable" in merged
    assert "- [x] Second deliverable" in merged
    assert "<<<<<<<" not in merged


def test_own_charter_merge_unions_events_jsonl(tmp_path):
    repo = _conflicted_repo(tmp_path)
    res = git_ops.rebase_worktree_onto_base(repo, base_branch="castle", own_quest_id=CHARTER_ID)

    assert res["ok"] is True and res["merged"] is True, res
    lines = [ln for ln in (repo / EVENTS_PATH).read_text(encoding="utf-8").splitlines() if ln]
    assert len(lines) == 3
    assert [json.loads(ln)["ts"] for ln in lines] == [
        "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z",
    ]
    values = {json.loads(ln)["value"] for ln in lines}
    assert values == {"PLANNED", "WORKING", "TRIBUTE_READY"}


def test_own_charter_merge_refuses_changed_expected_tribute_text(tmp_path):
    repo = _conflicted_repo(tmp_path, castle_items=("First deliverable", "Second deliverable (rewritten)"))
    res = git_ops.rebase_worktree_onto_base(repo, base_branch="castle", own_quest_id=CHARTER_ID)

    assert res["ok"] is False
    assert res["conflict"] is True
    assert CHARTER_PATH in res["conflicting_files"]
    assert "<<<<<<<" not in (repo / CHARTER_PATH).read_text(encoding="utf-8")


def test_own_paperwork_resolution_can_be_disabled(tmp_path):
    repo = _conflicted_repo(tmp_path, castle_checked=(2,))
    res = git_ops.rebase_worktree_onto_base(
        repo, base_branch="castle", own_quest_id=CHARTER_ID,
        auto_resolve_own_paperwork=False)

    assert res["ok"] is False
    assert res["conflict"] is True
    assert CHARTER_PATH in res["conflicting_files"]


def test_castle_only_sections_are_kept_verbatim_and_branch_tribute_survives(tmp_path):
    """A royal decree appends `## Audience Log` (and a MoC `## Commutation`) on
    castle after the branch was cut. Castle owns the structure, so the merge
    must not refuse — and the branch's rendered tribute still survives."""
    repo = _conflicted_repo(
        tmp_path,
        castle_extra=(("Audience Log", ""), ("Commutation", "Deploy the flag.")),
        castle_checked=(2,),
    )
    res = git_ops.rebase_worktree_onto_base(repo, base_branch="castle", own_quest_id=CHARTER_ID)

    assert res["ok"] is True and res["merged"] is True, res
    merged = (repo / CHARTER_PATH).read_text(encoding="utf-8")
    assert "The branch's rendered tribute." in merged
    assert "Deploy the flag." in merged
    positions = [
        merged.index("## Castle Ledger"),
        merged.index("## Audience Log"),
        merged.index("## Commutation"),
        merged.index("## Expected Tribute"),
        merged.index("## Tribute Rendered"),
    ]
    assert positions == sorted(positions)
    assert "<<<<<<<" not in merged


def test_a_branch_only_section_that_is_not_tribute_refuses(tmp_path):
    repo = _conflicted_repo(tmp_path, branch_extra=(("Royal Addendum", "Branch-only scope."),))
    res = git_ops.rebase_worktree_onto_base(repo, base_branch="castle", own_quest_id=CHARTER_ID)

    assert res["ok"] is False
    assert res["conflict"] is True
    assert CHARTER_PATH in res["conflicting_files"]


def test_branch_lacking_tribute_section_uses_castles_stale_block(tmp_path):
    repo = _conflicted_repo(
        tmp_path,
        branch_has_tribute=False,
        castle_tribute="## Ballad\n\nStale castle tribute.",
    )
    res = git_ops.rebase_worktree_onto_base(repo, base_branch="castle", own_quest_id=CHARTER_ID)

    assert res["ok"] is True and res["merged"] is True, res
    merged = (repo / CHARTER_PATH).read_text(encoding="utf-8")
    assert "Stale castle tribute." in merged
    assert "<<<<<<<" not in merged
