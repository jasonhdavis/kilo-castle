#!/usr/bin/env python3
"""
Court CLI — deterministic Quest/Epic ledger operations.

Usage examples:
    kilo-castle init
    court new --app api --concern auth-jwt-rotation --title "Rotate JWT secret keys" --section "Bug fix"
    court status
    court show Q001
    court advance Q001 WORKING --note "Serf dispatched"
    court rollup --section ballad --epic Q012
    court rollup --section penance --all
    court edict "Focus on queue reliability and reducing database compute hours"
    court ship --epic Q012
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

from court import branch_ops, git_ops, store, ward
from court.models import KINDS, SECTIONS, STATUSES, Quest, now_iso, validate_branch_name


def cmd_init(args):
    from court.init_cmd import run_init
    run_init(
        target_dir=Path(args.target_dir) if args.target_dir else Path.cwd(),
        force=args.force,
    )


def cmd_new(args):
    court_root = store.get_court_root()
    quest_id = store.make_id(args.app, args.concern, court_root=court_root)
    quest = Quest(
        id=quest_id,
        title=args.title,
        kind=args.kind,
        app=args.app,
        concern=args.concern,
        section=args.section or "",
        tags=args.tags or args.section or "",
        parent_epic=args.epic or "",
    )
    quest.branch = args.branch or quest.tree_branch
    if args.goal:
        quest.set_section("Goal & Scope", args.goal)
    if args.tribute:
        quest.set_section("Expected Tribute", args.tribute)
    quest.append_history("-", quest.status, "Quest created")
    path = store.save(quest, court_root=court_root)
    print(f"Created {quest.id} -> {path}")
    print(quest.to_markdown())


STATUS_GLYPHS = {
    "OPEN": "📋",
    "PLANNED": "📝",
    "DISPATCHED": "🚀",
    "WORKING": "⚙️",
    "REVIEW": "🪙",
    "GATE": "🛡️",
    "READY_FOR_TEARDOWN": "🪦",
    "DONE": "✅",
    "HELD": "⏸️",
    "PUNISHED": "🔒",
}


def cmd_show(args):
    court_root = store.get_court_root()
    quest = store.load(args.quest_id, court_root=court_root)
    print(quest.to_markdown())
    if quest.kind == "epic":
        hierarchy = store.get_hierarchy(include_archive=False, court_root=court_root)
        for epic, children in hierarchy["epics"]:
            if epic.id == quest.id or epic.id.split("-")[0].lower() == quest.id.split("-")[0].lower():
                if children:
                    completed = sum(1 for c in children if c.status in ("READY_FOR_TEARDOWN", "DONE"))
                    print(f"\n# Child Quests ({completed}/{len(children)} Complete)\n")
                    for j, child in enumerate(children):
                        is_last = (j == len(children) - 1)
                        pfx = "└── " if is_last else "├── "
                        glyph = STATUS_GLYPHS.get(child.status, "•")
                        serf = f"serf={child.serf_session_id}" if child.serf_session_id else ""
                        branch = f"branch={child.branch}" if child.branch else ""
                        meta = " ".join(filter(None, [branch, serf]))
                        meta_str = f" ({meta})" if meta else ""
                        print(f"{pfx}{glyph} [{child.status}] {child.id}{meta_str}")
                        print(f"{'    ' if is_last else '│   '}    {child.title}")
                break


def cmd_list(args):
    court_root = store.get_court_root()
    quests = store.list_all(include_archive=args.all, court_root=court_root)
    if args.status:
        quests = [q for q in quests if q.status == args.status]
    if args.app:
        quests = [q for q in quests if q.app == args.app]
    if not quests:
        print("(no matching quests)")
        return
    for q in quests:
        print(f"{q.id:38} [{q.status:19}] {q.section:12} {q.title[:60]}")


def render_tree_view(court_root: Optional[Path] = None, include_archive: bool = False) -> str:
    root = court_root or store.get_court_root()
    hierarchy = store.get_hierarchy(include_archive=include_archive, court_root=root)
    epics = hierarchy["epics"]
    standalone = hierarchy["standalone"]
    scouts = hierarchy["scouts"]

    lines = [
        "=" * 72,
        "THE COURT — Quest & Epic Hierarchy",
        "=" * 72,
    ]

    if epics:
        lines.append(f"\n🏰 EPICS ({len(epics)})")
        for i, (epic, children) in enumerate(epics):
            is_last_epic = (i == len(epics) - 1) and not standalone and not scouts
            prefix = "└── " if is_last_epic else "├── "
            indent = "    " if is_last_epic else "│   "

            completed_count = sum(1 for c in children if c.status in ("READY_FOR_TEARDOWN", "DONE"))
            total_count = len(children)
            progress = f"({completed_count}/{total_count} Quests Complete)" if total_count else "(No child Quests)"
            glyph = STATUS_GLYPHS.get(epic.status, "•")

            lines.append(f"{prefix}{glyph} [{epic.status}] {epic.id} {progress}")
            lines.append(f"{indent}    {epic.title}")

            for j, child in enumerate(children):
                is_last_child = (j == len(children) - 1)
                c_prefix = "└── " if is_last_child else "├── "
                c_glyph = STATUS_GLYPHS.get(child.status, "•")
                c_serf = f"serf={child.serf_session_id}" if child.serf_session_id else ""
                c_branch = f"branch={child.branch}" if child.branch else ""
                meta = " ".join(filter(None, [c_branch, c_serf]))
                meta_str = f" ({meta})" if meta else ""
                lines.append(f"{indent}{c_prefix}{c_glyph} [{child.status}] {child.id}{meta_str}")
                lines.append(f"{indent}{'    ' if is_last_child else '│   '}    {child.title}")

    if standalone:
        lines.append(f"\n⚔️ STANDALONE QUESTS ({len(standalone)})")
        for i, q in enumerate(standalone):
            is_last = (i == len(standalone) - 1) and not scouts
            prefix = "└── " if is_last else "├── "
            indent = "    " if is_last else "│   "
            glyph = STATUS_GLYPHS.get(q.status, "•")
            serf = f"serf={q.serf_session_id}" if q.serf_session_id else ""
            branch = f"branch={q.branch}" if q.branch else ""
            meta = " ".join(filter(None, [branch, serf]))
            meta_str = f" ({meta})" if meta else ""
            lines.append(f"{prefix}{glyph} [{q.status}] {q.id} [{q.section}]{meta_str}")
            lines.append(f"{indent}    {q.title}")

    if scouts:
        lines.append(f"\n🔭 SCOUTS & INVESTIGATIONS ({len(scouts)})")
        for i, q in enumerate(scouts):
            is_last = (i == len(scouts) - 1)
            prefix = "└── " if is_last else "├── "
            indent = "    " if is_last else "│   "
            glyph = STATUS_GLYPHS.get(q.status, "•")
            serf = f"serf={q.serf_session_id}" if q.serf_session_id else ""
            branch = f"branch={q.branch}" if q.branch else ""
            meta = " ".join(filter(None, [branch, serf]))
            meta_str = f" ({meta})" if meta else ""
            lines.append(f"{prefix}{glyph} [{q.status}] {q.id}{meta_str}")
            lines.append(f"{indent}    {q.title}")

    return "\n".join(lines)


def cmd_tree(args):
    court_root = store.get_court_root()
    print(render_tree_view(court_root=court_root, include_archive=args.all))


def agent_manager_json_path() -> Path:
    """Resolve `.kilo/agent-manager.json` at the MAIN repository root.

    Agent Manager keeps its state file in the primary checkout, not in linked
    git worktrees (`.kilo/` is per-checkout local state). Commands that read
    it (status orphan scan, raze, timber, teardown-list) must therefore
    resolve it via the shared git common dir — otherwise running `court` from
    inside a Quest worktree silently finds nothing. Falls back to the plain
    relative path when git metadata is unavailable.
    """
    local = Path(".kilo/agent-manager.json")
    try:
        res = git_ops._run(["git", "rev-parse", "--git-common-dir"], Path("."))
        common = (res.get("stdout") or "").strip() if res.get("ok") else ""
        if common:
            common_path = Path(common).resolve()
            candidate = common_path.parent / ".kilo" / "agent-manager.json"
            if candidate.exists():
                return candidate
    except Exception:
        pass
    return local


def find_orphaned_worktrees(court_root: Optional[Path] = None) -> list[dict]:
    """Cross-reference live Agent Manager worktrees against ACTIVE Court Quests.

    A worktree whose branch has no matching active Quest record is an orphan —
    most commonly a Quest (often a Scout) whose record was archived while its
    physical worktree/branch stayed live in Agent Manager, so it silently
    vanishes from `court status`'s default (active-only) view forever.
    """
    root = court_root or store.get_court_root()
    am_path = agent_manager_json_path()
    if not am_path.exists():
        return []
    try:
        am_data = json.loads(am_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    active_quests = store.list_all(include_archive=False, court_root=root)
    active_branches = {q.branch for q in active_quests if q.branch}
    all_quests = store.list_all(include_archive=True, court_root=root)
    branch_to_quest = {q.branch: q for q in all_quests if q.branch}

    orphans = []
    for wdata in am_data.get("worktrees", {}).values():
        branch = wdata.get("branch") or ""
        if not branch or branch in active_branches:
            continue
        # Persistent infrastructure branches are never Quest-linked by design.
        if branch in ("main", "castle") or branch.startswith("the-gatehouse"):
            continue
        matched = branch_to_quest.get(branch)
        orphans.append({
            "branch": branch,
            "path": wdata.get("path", ""),
            "quest_id": matched.id if matched else None,
            "quest_status": matched.status if matched else None,
        })
    return orphans


def cmd_status(args):
    court_root = store.get_court_root()
    if getattr(args, "tree", False):
        print(render_tree_view(court_root=court_root, include_archive=False))
        return

    quests = store.list_all(include_archive=False, court_root=court_root)
    if not quests:
        print("The Court is empty. No active Quests or Epics.")
        return

    groups: dict[str, list[Quest]] = {s: [] for s in STATUSES}
    for q in quests:
        groups.setdefault(q.status, []).append(q)

    order = [
        "HELD",
        "PUNISHED",
        "REVIEW",
        "GATE",
        "WORKING",
        "DISPATCHED",
        "PLANNED",
        "OPEN",
        "READY_FOR_TEARDOWN",
        "DONE",
    ]

    print("=" * 72)
    print("THE COURT — current state")
    print("=" * 72)
    for status in order:
        items = groups.get(status, [])
        if not items:
            continue
        print(f"\n[{status}] ({len(items)})")
        for q in items:
            serf = f"serf={q.serf_session_id}" if q.serf_session_id else "serf=-"
            branch = f"branch={q.branch}" if q.branch else "branch=-"
            print(f"  - {q.id:38} {q.kind:5} {branch:32} {serf}")
            print(f"      {q.title}")

    orphans = find_orphaned_worktrees(court_root=court_root)
    if orphans:
        print(f"\n⚠️  ORPHANED WORKTREES ({len(orphans)}) — live in Agent Manager, invisible above")
        for o in orphans:
            linked = f"{o['quest_id']} [{o['quest_status']}, archived]" if o["quest_id"] else "no linked Quest record at all"
            print(f"  - {o['branch']} ({o['path']}) -> {linked}")
        print("  Run `court timber` for full detail; `court raze <id>`/`court archive <id>` to close these out.")


def cmd_advance(args):
    court_root = store.get_court_root()
    raw_ids = args.quest_id.split(",") if "," in args.quest_id else [args.quest_id]
    for q_id in raw_ids:
        q_id = q_id.strip()
        if not q_id:
            continue
        quest = store.load(q_id, court_root=court_root)
        quest.set_status(args.status, args.note or "")
        store.save(quest, court_root=court_root)
        print(f"{quest.id}: {quest.status}")


def cmd_log(args):
    quest = store.load(args.quest_id)
    quest.append_history(quest.status, quest.status, args.note)
    store.save(quest)
    print(f"Logged note on {quest.id}")


def cmd_set_field(args):
    quest = store.load(args.quest_id)
    if not hasattr(quest, args.field):
        print(f"ERROR: unknown field {args.field!r}", file=sys.stderr)
        sys.exit(1)
    if args.field in ("id", "kind"):
        print("ERROR: cannot change id/kind with set-field", file=sys.stderr)
        sys.exit(1)
    setattr(quest, args.field, args.value)
    quest.updated_at = now_iso()
    store.save(quest)
    print(f"{quest.id}.{args.field} = {args.value}")


def cmd_set_section(args):
    quest = store.load(args.quest_id)
    if args.file:
        content = Path(args.file).read_text(encoding="utf-8")
    else:
        content = args.content or ""
    quest.set_section(args.section, content, mode=("append" if args.append else "replace"))
    store.save(quest)
    print(f"Updated section {args.section!r} on {quest.id}")


def cmd_verify(args):
    quest = store.load(args.quest_id)
    if not quest.worktree:
        print(f"ERROR: {quest.id} has no worktree path set (use set-field)", file=sys.stderr)
        sys.exit(1)

    report_lines = [f"## Verify run ({now_iso()})"]
    status = git_ops.worktree_status(quest.worktree)
    report_lines.append(f"- worktree_status: {status}")

    if args.test_cmd:
        test_result = git_ops.run_test_command(quest.worktree, args.test_cmd, timeout=args.timeout)
        report_lines.append(f"- test_cmd: `{args.test_cmd}`")
        report_lines.append(f"- exit_code: {test_result.get('exit_code')}")
        report_lines.append(f"- ok: {test_result.get('ok')}")
        if test_result.get("stdout"):
            report_lines.append(f"```\n{test_result['stdout'][-1500:]}\n```")
        if test_result.get("stderr"):
            report_lines.append(f"stderr:\n```\n{test_result['stderr'][-1500:]}\n```")

    quest.set_section("Tribute Rendered", "\n".join(report_lines), mode="append")
    quest.append_history(quest.status, quest.status, "Deterministic verify run recorded")
    store.save(quest)
    print("\n".join(report_lines))


def cmd_verify_merged(args):
    court_root = store.get_court_root()
    quest = store.load(args.quest_id, court_root=court_root)
    branch = quest.branch or quest.worktree or ""
    if not branch:
        print(f"ERROR: {quest.id} has no branch or worktree set", file=sys.stderr)
        sys.exit(1)

    repo_root = court_root.parent
    status = git_ops.check_merged_status(branch, target_ref="castle", base_ref="castle", cwd=repo_root)
    print(f"Merge check for {quest.id} ({branch}):")
    print(f"  - is_merged_into_castle: {status.get('is_merged_in_target') or status.get('is_merged_in_base')}")
    print(f"  - unmerged_commits: {status.get('unmerged_commits_count', 0)}")
    print(f"  - clean_worktree: {status.get('clean_worktree')}")
    if status.get("uncommitted_files"):
        print(f"  - uncommitted_files ({len(status['uncommitted_files'])}):")
        for uf in status["uncommitted_files"][:5]:
            print(f"      {uf}")
    print(f"  - recommendation: {status.get('recommendation')}")

    if args.sync and status.get("worktree") and Path(status["worktree"]).exists():
        wt_path = Path(status["worktree"])
        sync_res = git_ops._run(["git", "merge", "castle", "--ff-only"], wt_path)
        if sync_res.get("ok"):
            print(f"  - synced_to_castle: OK (fast-forwarded)")
        else:
            print(f"  - synced_to_castle: FAILED ({sync_res.get('stderr')})")


def cmd_raze(args):
    import json
    court_root = store.get_court_root()
    repo_root = court_root.parent
    am_path = repo_root / ".kilo" / "agent-manager.json"
    am_data = {}
    if am_path.exists():
        try:
            am_data = json.loads(am_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    am_wts = am_data.get("worktrees", {})
    am_sessions = am_data.get("sessions", {})
    ashes_section_id = None
    for sec_id, sec_data in am_data.get("sections", {}).items():
        if sec_data.get("name") == "Ashes":
            ashes_section_id = sec_id
            break

    target_ids = []
    if args.quest_id.lower() in ("all", "all-ready", "ready"):
        target_ids = [q.id for q in store.list_all(court_root=court_root) if q.status in ("GATE", "READY_FOR_TEARDOWN", "REVIEW")]
    else:
        target_ids = [args.quest_id]

    if not target_ids:
        print("(no candidate quests found to raze)")
        return

    for qid in target_ids:
        try:
            quest = store.load(qid, court_root=court_root)
        except Exception as e:
            print(f"Skipping {qid}: {e}")
            continue

        branch = quest.branch or ""
        wt_path_str = quest.worktree or ""
        found_wt_id = None
        found_wt_path = None
        found_session_id = None

        for wid, wdata in am_wts.items():
            if wdata.get("branch") == branch or (wt_path_str and (wdata.get("path") == wt_path_str or wid == wt_path_str)):
                found_wt_id = wid
                found_wt_path = wdata.get("path")
                break

        if found_wt_id:
            for sid, sdata in am_sessions.items():
                if sdata.get("worktreeId") == found_wt_id:
                    found_session_id = sid
                    break

        wt_exists = bool(found_wt_path and Path(found_wt_path).exists())

        if not wt_exists and not found_wt_id:
            if quest.status == "READY_FOR_TEARDOWN" and args.archive_pruned:
                dst = store.archive(quest.id, court_root=court_root)
                print(f"🪦 {quest.id}: Worktree already pruned from disk/AM -> Archived to {dst}")
                continue
            elif quest.status == "READY_FOR_TEARDOWN":
                print(f"ℹ️ {quest.id}: Worktree already pruned from disk/AM (ready to archive: court archive {quest.id})")
                continue

        status = git_ops.check_merged_status(found_wt_path or branch, target_ref="castle", base_ref="castle", cwd=repo_root)
        is_merged = status.get("is_merged_in_target") or status.get("is_merged_in_base")

        if wt_exists and found_wt_path:
            p = Path(found_wt_path)
            st_res = git_ops._run(["git", "status", "--porcelain"], p)
            if st_res.get("ok") and st_res.get("stdout"):
                print(f"⚠️ {quest.id}: Worktree {found_wt_path} is dirty with uncommitted changes! Clean before razing.")
                continue

            ff_res = git_ops._run(["git", "merge", "castle", "--ff-only"], p)
            if not ff_res.get("ok"):
                if is_merged:
                    git_ops._run(["git", "reset", "--hard", "castle"], p)

        if quest.status != "READY_FOR_TEARDOWN":
            quest.append_history(quest.status, "READY_FOR_TEARDOWN", "Razed: verified merged, synced to castle (ahead: 0, behind: 0), queued for teardown in Ashes")
            quest.status = "READY_FOR_TEARDOWN"
            store.save(quest, court_root=court_root)
            print(f"✅ Advanced {quest.id} -> READY_FOR_TEARDOWN")

        print(f"🔥 Razed {quest.id}:")
        print(f"   - Branch: {branch}")
        print(f"   - Worktree: {found_wt_id} ({found_wt_path})")
        print(f"   - Session ID: {found_session_id or quest.serf_session_id or 'None'}")
        print(f"   - Ashes Section ID: {ashes_section_id}")
        if found_session_id and ashes_section_id:
            print(f"   👉 Move command: agent_manager move sessionID: {found_session_id} sectionID: {ashes_section_id}")


def cmd_teardown_list(args):
    import json
    court_root = store.get_court_root()
    repo_root = court_root.parent
    am_path = repo_root / ".kilo" / "agent-manager.json"
    am_data = {}
    if am_path.exists():
        try:
            am_data = json.loads(am_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    am_wts = am_data.get("worktrees", {})
    ashes_section_id = None
    for sec_id, sec_data in am_data.get("sections", {}).items():
        if sec_data.get("name") == "Ashes":
            ashes_section_id = sec_id
            break

    quests = [q for q in store.list_all(court_root=court_root) if q.status == "READY_FOR_TEARDOWN"]
    if not quests:
        print("(nothing queued for teardown)")
        return

    active_in_ashes = []
    active_other_lane = []
    already_pruned = []

    for q in quests:
        branch = q.branch or ""
        found_wt_id = None
        found_wt_info = None
        for wid, wdata in am_wts.items():
            if wdata.get("branch") == branch or wid == q.worktree:
                found_wt_id = wid
                found_wt_info = wdata
                break

        if not found_wt_id or not found_wt_info or not Path(found_wt_info.get("path", "")).exists():
            already_pruned.append(q)
            continue

        wt_path = found_wt_info.get("path", "")
        sec_id = found_wt_info.get("sectionId", "")

        diff_res = git_ops._run(["git", "-C", wt_path, "rev-list", "--left-right", "--count", f"castle...{branch}"], repo_root)
        behind, ahead = "0", "0"
        if diff_res.get("ok") and diff_res.get("stdout"):
            parts = diff_res["stdout"].split()
            if len(parts) == 2:
                behind, ahead = parts[0], parts[1]

        st_res = git_ops._run(["git", "-C", wt_path, "status", "--porcelain"], repo_root)
        dirty_count = len(st_res.get("stdout", "").splitlines()) if st_res.get("stdout") else 0

        info = {
            "quest": q,
            "wt_id": found_wt_id,
            "path": wt_path,
            "sec_id": sec_id,
            "behind": behind,
            "ahead": ahead,
            "dirty_count": dirty_count,
        }

        if sec_id == ashes_section_id:
            active_in_ashes.append(info)
        else:
            active_other_lane.append(info)

    print("=" * 76)
    print(f"🪦 THE COURT TEARDOWN LIST — WORKTREES AWAITING DELETION IN ASHES")
    print("=" * 76)

    if active_in_ashes:
        print(f"\n🔥 Resting in Ashes Section (Safe for M'Lord to delete in Agent Manager UI) ({len(active_in_ashes)}):")
        for item in active_in_ashes:
            q = item["quest"]
            aligned = "✅ Aligned (0 drift)" if item["behind"] == "0" and item["ahead"] == "0" and item["dirty_count"] == 0 else f"⚠️ Drift (behind={item['behind']}, ahead={item['ahead']}, dirty={item['dirty_count']})"
            print(f"   * {q.id}: {item['wt_id']} ({item['path']}) [{aligned}]")

    if active_other_lane:
        print(f"\n⚠️ In READY_FOR_TEARDOWN but not yet moved to Ashes ({len(active_other_lane)}):")
        for item in active_other_lane:
            q = item["quest"]
            print(f"   * {q.id}: {item['wt_id']} ({item['path']}) [Section: {item['sec_id']}]")

    if already_pruned:
        print(f"\n📦 Already Pruned from Agent Manager / Disk ({len(already_pruned)} Quests ready to archive):")
        for q in already_pruned:
            print(f"   * {q.id} (branch={q.branch or '-'})")

    print("\n" + "=" * 76)


def cmd_archive(args):
    dst = store.archive(args.quest_id)
    print(f"Archived -> {dst}")


def cmd_rollup(args):
    results = store.rollup_section(
        section_name=args.section,
        app=args.app,
        epic=args.epic,
        status=args.status,
        include_archive=args.all,
    )
    if not results:
        print(f"(no rendered {args.section} found matching filters)")
        return

    print("=" * 72)
    print(f"COURT ROLLUP — {args.section.upper()} ({len(results)} Quests)")
    print("=" * 72)
    for quest, content in results:
        print(f"\n### {quest.id} ({quest.title}) [{quest.status}]")
        print(content)


def cmd_tally(args):
    results = store.rollup_section(
        section_name="tally",
        app=args.app,
        epic=args.epic,
        status=args.status,
        include_archive=args.all,
    )
    if not results:
        print("(no rendered production verification runbooks/tallies found matching filters)")
        return

    print("=" * 76)
    print(f"🔍 THE COURT TALLY — PRODUCTION & UI VERIFICATION RUNBOOKS ({len(results)} Quests)")
    print("=" * 76)
    for quest, content in results:
        print(f"\n### {quest.id}: {quest.title} ({quest.app}) [{quest.status}]")
        print(content)


def cmd_edict(args):
    current = store.load_edicts()
    if args.content or args.file:
        new_text = Path(args.file).read_text(encoding="utf-8") if args.file else args.content
        if args.append and current:
            updated = current + "\n\n" + f"- **{now_iso()[:10]}**: {new_text.strip()}"
        else:
            updated = f"# Royal Edicts & Decrees\n\n- **{now_iso()[:10]}**: {new_text.strip()}"
        store.save_edicts(updated)
        print(f"Updated .court/EDICTS.md")
    else:
        if current:
            print(current)
        else:
            print("(no royal edicts recorded; add with: court edict 'Your priority')")


def cmd_ship(args):
    """Cog Ship: the convoy of tribute entering the castle. Deterministically
    combines the Bard/Coffers/Atone/Murmur rollups for the Quest convoy plus
    the raw base..head git promotion vector (e.g. main..castle)."""
    court_root = store.get_court_root()
    base_branch = getattr(args, "base", "main")
    head_branch = getattr(args, "head", "castle")

    manifest = store.rollup_ship_manifest(
        app=args.app,
        epic=args.epic,
        status=args.status,
        include_archive=args.all,
        court_root=court_root,
    )
    quests = manifest["quests"]

    repo_root = court_root.parent
    ab = git_ops.get_ahead_behind(base_branch, head_branch, cwd=repo_root)
    log_res = git_ops.get_branch_log(base_branch, head_branch, max_count=30, cwd=repo_root)
    diff_res = git_ops.get_branch_diffstat(base_branch, head_branch, cwd=repo_root)

    print("=" * 76)
    print(f"🚢 COG SHIP DEPLOYMENT CONVOY — TRIBUTE ENTERING THE CASTLE ({len(quests)} Quests)")
    print("=" * 76)

    if ab.get("ok"):
        ahead = ab.get("ahead", 0)
        behind = ab.get("behind", 0)
        print(f"\n🏰 Branch Promotion Vector: {head_branch} -> {base_branch}")
        print(f"   * Status: {head_branch} is {ahead} commits ahead, {behind} commits behind {base_branch}")
        if log_res.get("ok") and log_res.get("stdout"):
            log_lines = log_res["stdout"].splitlines()
            print(f"\n📦 Shipped Commits on {head_branch} ahead of {base_branch}:")
            for line in log_lines[:20]:
                print(f"   - {line}")
            if len(log_lines) > 20:
                print(f"   ... ({len(log_lines) - 20} more commits)")
        if diff_res.get("ok") and diff_res.get("stdout"):
            print(f"\n📊 Aggregate Diffstat ({base_branch}..{head_branch}):")
            for line in diff_res["stdout"].splitlines()[-5:]:
                print(f"   {line}")
    else:
        print(f"\n🏰 Branch Promotion Vector: {head_branch} (ready for deployment)")

    if quests:
        print(f"\n📋 Quests in Deployment Convoy ({len(quests)}):")
        for q in quests:
            parent_info = f" [Epic: {q.parent_epic}]" if q.parent_epic else ""
            print(f"   * {q.id} ({q.app}): {q.title} [{q.status}]{parent_info}")
    else:
        print("\n(no quests currently in READY_FOR_TEARDOWN or DONE matching filters)")

    if manifest["ballads"]:
        print("\n" + "-" * 76)
        print(f"📜 THE BARD'S CHRONICLE — Narrative & Transformations ({len(manifest['ballads'])} Ballads)")
        print("-" * 76)
        for q, b in manifest["ballads"]:
            print(f"\n### {q.id}: {q.title} ({q.app})")
            print(b)

    if manifest["tributes"]:
        print("\n" + "-" * 76)
        print(f"💰 THE COFFERS LEDGER — Provable Deliverables & Commits ({len(manifest['tributes'])} Tributes)")
        print("-" * 76)
        for q, t in manifest["tributes"]:
            print(f"\n### {q.id}: {q.title} ({q.app})")
            print(t)

    if manifest["tallies"]:
        print("\n" + "-" * 76)
        print(f"🔍 THE TALLY RUNBOOK — Production Verification & UI Paths ({len(manifest['tallies'])} Tallies)")
        print("-" * 76)
        for q, v in manifest["tallies"]:
            print(f"\n### {q.id}: {q.title} ({q.app})")
            print(v)

    if manifest["penances"]:
        print("\n" + "-" * 76)
        print(f"⚖️ THE SERF PENANCE — Technical Debt & Remediations ({len(manifest['penances'])} Penances)")
        print("-" * 76)
        for q, p_ in manifest["penances"]:
            print(f"\n### {q.id}: {q.title} ({q.app})")
            print(p_)

    if manifest["opinions"]:
        print("\n" + "-" * 76)
        print(f"💡 THE HUMBLE OPINIONS — Field Intelligence & Next Quests ({len(manifest['opinions'])} Opinions)")
        print("-" * 76)
        for q, o in manifest["opinions"]:
            print(f"\n### {q.id}: {q.title} ({q.app})")
            print(o)

    print("\n" + "=" * 76)
    print("🏰 COG SHIP DEPLOYMENT SUMMARY COMPLETE")
    print("=" * 76)


def cmd_pillory(args):
    """Send a Quest to the pillory (alias: `punish`).

    Unconditional and automatic: no proof of landing means straight to
    PUNISHED with Decrees; a Quest is never returned to WORKING from here.
    The one courtesy check is proof-of-landing — if the work actually did
    land somewhere despite appearances, don't waste a whole new Quest+worktree
    ceremony on it.
    """
    court_root = store.get_court_root()
    quest = store.load(args.quest_id, court_root=court_root)
    proof = git_ops.check_proof_of_landing(quest, cwd=court_root.parent)
    if proof.get("landed"):
        sha = proof.get("proof_commit", "unknown")
        ref = proof.get("matched_ref", "unknown")
        note = f"Pillory check: found proof of landing on {ref} (commit {sha}); no punishment needed."
        quest.append_history(quest.status, quest.status, note)
        store.save(quest, court_root=court_root)
        print(f"{quest.id}: Work found landed on {ref} (commit {sha}). Status remains [{quest.status}].")
        return

    reason = args.reason or "Master of Coin audit rejected this Quest's tribute."
    decrees = args.decrees or "(no decrees recorded — Steward must supply before chartering the successor)"
    old_status = quest.status
    quest.set_status("PUNISHED", f"Punished: {reason}")
    quest.set_section(
        "Judgement of the Condemned",
        "\n".join([
            f"- **Reason:** {reason}",
            f"- **Decrees Issued:** {decrees}",
            f"- **Successor Quest:** {args.successor or '(pending — charter one now)'}",
            f"- **Frozen Worktree:** {quest.worktree or '-'} (read-only; no Serf re-enters it)",
            f"- **Raze Together With:** {args.successor or '(set once successor is chartered)'} (both razed in one pass once the successor completes)",
        ]),
    )
    if args.successor:
        quest.pilloried_by = args.successor
    store.save(quest, court_root=court_root)
    print(f"{quest.id}: Sent to the pillory. Status set to [PUNISHED]. Was: [{old_status}].")
    print(f"    Reason: {reason}")
    print(f"    Decrees: {decrees}")
    if not args.successor:
        print("    Next: charter a new Quest with these Decrees seeded into its Goal & Scope, then:")
        print(f"      court set-field {quest.id} pilloried_by <new_id>")
        print(f"      court set-field <new_id> pillory_of {quest.id}")


def cmd_stamp(args):
    """Stamp a batch of Quests onto one Cog Ship convoy id (cogship-NNN)."""
    court_root = store.get_court_root()
    ids = [s.strip() for s in args.quest_ids.split(",") if s.strip()]
    if not ids:
        print("ERROR: no quest ids given", file=sys.stderr)
        sys.exit(1)
    quests = []
    for qid in ids:
        try:
            quests.append(store.load(qid, court_root=court_root))
        except Exception as e:
            print(f"ERROR: {qid}: {e}", file=sys.stderr)
            sys.exit(1)
    cogship_arg = getattr(args, "cogship", None)
    cogship_id = None if (not cogship_arg or cogship_arg.lower() == "new") else cogship_arg
    auto_commit = getattr(args, "commit", False)
    try:
        stamped = store.stamp_cogship(quests, cogship_id=cogship_id, court_root=court_root, auto_commit=auto_commit)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Stamped {len(quests)} Quest(s) onto {stamped}:")
    for q in quests:
        print(f"   * {q.id} [{q.status}]")


def cmd_timber(args):
    court_root = store.get_court_root()
    repo_root = court_root.parent
    am_path = agent_manager_json_path()
    am_data = {}
    if am_path.exists():
        try:
            am_data = json.loads(am_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    am_wts = am_data.get("worktrees", {})
    am_sessions = am_data.get("sessions", {})
    sections = am_data.get("sections", {})
    section_map = {sec_id: sdata.get("name") for sec_id, sdata in sections.items()}

    git_wts = git_ops.list_git_worktrees(repo_root)
    quests = store.list_all(include_archive=True, court_root=court_root)

    branch_to_quest = {q.branch: q for q in quests if q.branch}

    if getattr(args, "json", False):
        out = []
        for wt in git_wts:
            wt_path = wt.get("worktree", "")
            branch = wt.get("branch", "")
            matched_quest = branch_to_quest.get(branch)
            am_wt_meta = None
            am_session_meta = None
            for wid, wdata in am_wts.items():
                if wdata.get("path") == wt_path or wdata.get("branch") == branch:
                    am_wt_meta = wdata
                    break
            if am_wt_meta:
                wid = [k for k, v in am_wts.items() if v == am_wt_meta][0]
                for sid, sdata in am_sessions.items():
                    if sdata.get("worktreeId") == wid:
                        am_session_meta = sdata
                        break
            out.append({
                "worktree": wt_path,
                "branch": branch,
                "section": section_map.get(am_wt_meta.get("sectionId"), "Ungrouped") if am_wt_meta else "None",
                "quest_id": matched_quest.id if matched_quest else None,
                "quest_status": matched_quest.status if matched_quest else None,
                "session": am_session_meta.get("name") if am_session_meta else None,
            })
        print(json.dumps(out, indent=2))
        return

    print("=" * 78)
    print("🌲 PHYSICAL GIT WORKTREES & AGENT MANAGER REALITY MAPPING")
    print("=" * 78)
    print(f"Total Physical Worktrees on Disk: {len(git_wts)}\n")

    for wt in git_wts:
        wt_path = wt.get("worktree", "")
        branch = wt.get("branch", "")
        branch_ref = wt.get("branch_ref", "")

        matched_quest = branch_to_quest.get(branch)
        if not matched_quest:
            for q in quests:
                if q.worktree and Path(q.worktree).resolve() == Path(wt_path).resolve():
                    matched_quest = q
                    break

        am_wt_meta = None
        am_session_meta = None
        for wid, wdata in am_wts.items():
            if wdata.get("path") == wt_path or wdata.get("branch") == branch:
                am_wt_meta = wdata
                break

        if am_wt_meta:
            wid = [k for k, v in am_wts.items() if v == am_wt_meta][0]
            for sid, sdata in am_sessions.items():
                if sdata.get("worktreeId") == wid:
                    am_session_meta = sdata
                    break

        section_name = section_map.get(am_wt_meta.get("sectionId"), "Ungrouped") if am_wt_meta else "External / None"
        quest_str = f"{matched_quest.id} [{matched_quest.status}] - {matched_quest.title}" if matched_quest else "No linked Quest"

        print(f"📁 {wt_path}")
        print(f"   • Branch:   {branch or branch_ref or 'detached'}")
        print(f"   • Section:  {section_name}")
        print(f"   • Quest:    {quest_str}")
        if am_session_meta:
            print(f"   • Session:  {am_session_meta.get('name', am_session_meta.get('id'))} [{am_session_meta.get('activity', 'idle')}]")
        print("-" * 78)


def cmd_audit(args):
    court_root = store.get_court_root()
    repo_root = court_root.parent
    base_branch = getattr(args, "base", "castle") or "castle"
    if getattr(args, "quest_id", None):
        try:
            quest = store.load(args.quest_id, court_root=court_root)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        res = ward.audit_quest(quest, base_branch=base_branch, cwd=repo_root, court_root=court_root)
        if getattr(args, "json", False):
            print(json.dumps(res.to_dict(), indent=2))
        else:
            print("=" * 76)
            print(res.format_report())
            print("=" * 76)
    else:
        results = ward.audit_all_quests(
            status=args.status,
            app=args.app,
            epic=args.epic,
            include_archive=args.all,
            base_branch=base_branch,
            cwd=repo_root,
            court_root=court_root,
        )
        if getattr(args, "json", False):
            print(json.dumps([r.to_dict() for r in results], indent=2))
            return

        if not results:
            print("(no quests matching filters)")
            return

        print("=" * 76)
        print(f"THE WARD — WORKTREE & TRIBUTE AUDIT ({len(results)} Quests)")
        print("=" * 76)
        for r in results:
            print()
            print(r.format_report())
            print("-" * 76)


def cmd_levy(args):
    court_root = store.get_court_root()
    repo_root = court_root.parent
    base_branch = getattr(args, "base", "castle") or "castle"
    target_status = args.status
    if not target_status and not getattr(args, "quest_id", None) and not args.all:
        target_status = "WORKING,DISPATCHED"

    if getattr(args, "quest_id", None):
        try:
            target_quests = [store.load(args.quest_id, court_root=court_root)]
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        target_quests = store.list_all(include_archive=args.all, court_root=court_root)
        if target_status:
            status_set = {s.strip().upper() for s in target_status.split(",") if s.strip()}
            target_quests = [q for q in target_quests if q.status in status_set]
        if args.app:
            target_quests = [q for q in target_quests if q.app.lower() == args.app.lower()]
        if args.epic:
            epic_norm = args.epic.lower().lstrip("q").partition("-")[0]
            target_quests = [
                q for q in target_quests
                if q.parent_epic.lower().lstrip("q").partition("-")[0] == epic_norm
                or q.id.lower().lstrip("q").partition("-")[0] == epic_norm
            ]

    if not target_quests:
        print("(no active quests found matching levy criteria)")
        return

    # Mechanical Per-Quest Rebase-Then-Advance.
    #
    # `base_branch` (default: castle) is a constantly-moving target — every
    # Gatekeeper Cog Ship promotion advances it — while the deferred-rebase
    # convention expects an idle Serf *agent* to notice drift and run
    # `git merge <base_branch>` itself before REVIEW. At real work-in-progress
    # volume that notice-and-spawn-an-agent round-trip is slower than the base
    # branch's advance rate, so drift only ever grows and the WORKING/REVIEW
    # queue can never converge.
    #
    # A subtler version of the same race exists *within a single levy run*:
    # advancing quest A to REVIEW saves onto disk (and, if auto-commit is in
    # play, commits) which can move the base branch's tip by one commit if
    # levy itself runs from the base branch — so if the mechanical rebase ran
    # as one batch pre-flight step before the advance loop, quest B (processed
    # right after A in the same run) would see fresh drift again by the time
    # its own audit runs, even though it was perfectly synced moments earlier.
    # So the rebase for each quest must happen immediately before *that
    # quest's own* audit/advance decision, not once for the whole batch up
    # front — every quest gets rebased against the base branch's tip as of
    # the moment it's actually decided, including any advance an earlier
    # quest in this same loop just took.
    rebase_notes: dict[str, str] = {}
    levied_list = []
    working_list = []
    violations_list = []

    for q in target_quests:
        # Step 0: Mechanical rebase onto base_branch, right before this
        # specific quest's own audit/advance decision (see note above).
        if getattr(args, "rebase", True) and q.status in ("WORKING", "REVIEW", "DISPATCHED"):
            wt = None
            if q.worktree and Path(q.worktree).is_dir():
                wt = Path(q.worktree)
            else:
                wt = git_ops.find_worktree_for_quest(q, cwd=repo_root)
            if wt and Path(wt).is_dir():
                rb = git_ops.rebase_worktree_onto_base(wt, base_branch=base_branch, own_quest_id=q.id)
                if rb.get("merged"):
                    auto_resolved = rb.get("auto_resolved_foreign_ledger_files") or []
                    foreign_note = f" (auto-resolved {len(auto_resolved)} foreign ledger conflict(s) to {base_branch}'s side)" if auto_resolved else ""
                    rebase_notes[q.id] = (
                        f"Mechanically rebased onto {base_branch}: "
                        f"{rb.get('before_behind')} -> {rb.get('after_behind')} behind.{foreign_note}"
                    )
                elif rb.get("conflict"):
                    sample = ", ".join(rb.get("conflict_files", [])[:5]) or "unknown files"
                    rebase_notes[q.id] = (
                        f"Mechanical rebase onto {base_branch} hit conflicts in: {sample} "
                        f"— aborted cleanly, needs manual resolution (`git merge {base_branch}` by hand)."
                    )
                elif rb.get("skipped") == "dirty":
                    rebase_notes[q.id] = "Skipped mechanical rebase: worktree has uncommitted changes."

        # Step 1: Auto-sync tribute and frontmatter from worktree if present
        if getattr(args, "sync", True):
            ward.sync_tribute_from_worktree(q, cwd=repo_root, court_root=court_root)

        # Step 2: Audit — freshly re-reads git status now, so it sees the
        # rebase this quest just got (and any base-branch advance from an
        # earlier quest in this same loop), not a stale pre-flight snapshot.
        audit = ward.audit_quest(q, base_branch=base_branch, cwd=repo_root, court_root=court_root)

        # Step 3: Advance if requested and compliant
        did_advance = False
        if args.advance and audit.is_compliant and audit.tribute_present and q.status in ("WORKING", "DISPATCHED"):
            # Drift behind the base branch is tolerated during WORKING, but
            # REVIEW entry requires exactly one clean rebase (behind == 0).
            # The audit above ran under WORKING rules where behind > 0 is
            # only a warning — without this gate, --advance would promote a
            # drifted quest into REVIEW and instantly mint a violation.
            behind = audit.git_status.get("behind")
            if behind is not None and behind > 0:
                audit.warnings.append(
                    f"Blocked auto-advance to REVIEW: {behind} commit(s) behind {base_branch} "
                    f"(run `court rebase {q.id}` or resolve manually first)."
                )
            else:
                q.set_status("REVIEW", "Levied: Tribute synced from worktree and verified compliant")
                store.save(q, court_root=court_root)
                did_advance = True
                audit.status = "REVIEW"

        if audit.violations:
            violations_list.append((q, audit))
        elif audit.status == "REVIEW" or did_advance:
            levied_list.append((q, audit, did_advance))
        else:
            working_list.append((q, audit))

    if rebase_notes and not getattr(args, "json", False):
        print("MECHANICAL REBASE SWEEP (per-quest, interleaved with advance)")
        for qid, note in rebase_notes.items():
            print(f"  {qid}: {note}")
        print()

    if getattr(args, "json", False):
        out_data = {
            "rebase_sweep": rebase_notes,
            "levied": [a.to_dict() for _, a, _ in levied_list],
            "working": [a.to_dict() for _, a in working_list],
            "non_compliant": [a.to_dict() for _, a in violations_list],
        }
        print(json.dumps(out_data, indent=2))
        return

    print("=" * 78)
    print("THE COURT LEVY — DETERMINISTIC WORKTREE TRIAGE & TRIBUTE SYNCHRONIZATION")
    print("=" * 78)
    print(f"Summary: {len(levied_list)} Levied (REVIEW) | {len(working_list)} Working | {len(violations_list)} Action Needed / Blocked\n")

    if levied_list:
        print(f"LEVIED & READY FOR REVIEW ({len(levied_list)})")
        for q, audit, advanced in levied_list:
            adv_str = " -> [REVIEW] (Auto-advanced)" if advanced else " [REVIEW]"
            sec_str = f"({len(audit.sections_present)} sections verified)"
            ahead_str = f"↑{audit.git_status.get('ahead', 0)}" if audit.git_status.get("ahead") is not None else ""
            task_str = f" [Tasks: {audit.task_progress['summary']}]" if audit.task_progress.get("found") else ""
            phase_str = f" | Phase: {audit.task_progress['active_phase']}" if audit.task_progress.get("active_phase") else ""
            print(f"  ✓ {q.id:36} {adv_str:28} {ahead_str:5} {sec_str}{task_str}{phase_str}")
            print(f"      {q.title}")

    if working_list:
        print(f"\nACTIVE WORKING WORKTREES ({len(working_list)})")
        for q, audit in working_list:
            req_total = 5 if (q.kind == "scout" or q.section == "Investigation") else 6
            tribute_str = f"tribute: {len(audit.sections_present)}/{req_total} sections" if audit.tribute_present else "in-progress"
            ahead_str = f"↑{audit.git_status.get('ahead', 0)}" if audit.git_status.get("ahead") is not None else ""
            task_str = f"[Tasks: {audit.task_progress['summary']}] " if audit.task_progress.get("found") else ""
            phase_str = f" | Phase: {audit.task_progress['active_phase']}" if audit.task_progress.get("active_phase") else ""
            print(f"  • {q.id:36} [{q.status:10}] {task_str}{ahead_str:5} ({tribute_str}){phase_str}")
            print(f"      {q.title}")

    if violations_list:
        print(f"\nNON-COMPLIANT / DRIFTED / ACTION NEEDED ({len(violations_list)})")
        for q, audit in violations_list:
            behind_str = f"↓{audit.git_status.get('behind', 0)} behind" if audit.git_status.get("behind") else ""
            dirty_str = "DIRTY" if audit.git_status.get("dirty") else ""
            flags = " ".join(filter(None, [behind_str, dirty_str]))
            flags_str = f" [{flags}]" if flags else ""
            task_str = f" [Tasks: {audit.task_progress['summary']}]" if audit.task_progress.get("found") else ""
            print(f"  ✗ {q.id:36} [{q.status:10}]{flags_str}{task_str}")
            print(f"      {q.title}")
            for v in audit.violations:
                print(f"      - {v}")
            for w in audit.warnings:
                print(f"      - {w}")

    print("\n" + "=" * 78)
    print("RECOMMENDED NEXT STEPS & ACTIONS:")
    print("  • For idle working Quests / Serfs: Run `/goad <quest_id>` or prod the session via Agent Manager to update charter & continue.")
    print("  • For completed Quests in Review: Summon Master of Coin via `/review`.")
    print("  • For Quests ready for Gatehouse integration: Run `/collect`.")
    print("  • For a real merge conflict flagged above: dispatch the Serf to resolve it, then re-run `court rebase <id>`.")
    print("=" * 78)


def cmd_rebase(args):
    """Mechanically converge worktrees onto --base (default castle) via `git merge`,
    with zero agent turns involved. `court levy` runs this sweep automatically as a
    pre-flight step; this standalone verb exists for direct/manual remediation (e.g.
    targeting GATE Quests, or a single stuck Quest) without waiting for a full levy pass.
    """
    court_root = store.get_court_root()
    repo_root = court_root.parent
    base_branch = getattr(args, "base", "castle") or "castle"

    if getattr(args, "quest_id", None):
        try:
            target_quests = [store.load(args.quest_id, court_root=court_root)]
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        target_quests = store.list_all(include_archive=args.all, court_root=court_root)
        status_filter = args.status or "WORKING,REVIEW,DISPATCHED,GATE"
        status_set = {s.strip().upper() for s in status_filter.split(",") if s.strip()}
        target_quests = [q for q in target_quests if q.status in status_set]
        if args.app:
            target_quests = [q for q in target_quests if q.app.lower() == args.app.lower()]
        if args.epic:
            epic_norm = args.epic.lower().lstrip("q").partition("-")[0]
            target_quests = [
                q for q in target_quests
                if q.parent_epic.lower().lstrip("q").partition("-")[0] == epic_norm
                or q.id.lower().lstrip("q").partition("-")[0] == epic_norm
            ]

    if not target_quests:
        print("(no quests found matching rebase criteria)")
        return

    results = []
    for q in target_quests:
        wt = None
        if q.worktree and Path(q.worktree).is_dir():
            wt = Path(q.worktree)
        else:
            wt = git_ops.find_worktree_for_quest(q, cwd=repo_root)
        if not wt or not Path(wt).is_dir():
            results.append({"quest_id": q.id, "title": q.title, "skipped": "no-worktree"})
            continue

        if getattr(args, "dry_run", False):
            ab = git_ops.get_ahead_behind(base_branch, "HEAD", cwd=Path(wt))
            results.append({
                "quest_id": q.id, "title": q.title, "dry_run": True,
                "behind": ab.get("behind"), "ahead": ab.get("ahead"),
            })
            continue

        rb = git_ops.rebase_worktree_onto_base(wt, base_branch=base_branch, own_quest_id=q.id)
        rb["quest_id"] = q.id
        rb["title"] = q.title
        results.append(rb)

    if getattr(args, "json", False):
        print(json.dumps(results, indent=2))
        return

    print("=" * 78)
    print(f"THE COURT REBASE — MECHANICAL CONVERGENCE ONTO '{base_branch}' ({len(results)} Quests)")
    print("=" * 78)
    for r in results:
        qid = r.get("quest_id", "?")
        title = r.get("title", "")
        if r.get("skipped") == "no-worktree":
            print(f"  ⏭️  {qid:36} (no resolvable worktree — skipped)")
        elif r.get("dry_run"):
            print(f"  🔎 {qid:36} behind={r.get('behind')} ahead={r.get('ahead')}  {title}")
        elif r.get("skipped") == "dirty":
            print(f"  ⚠️  {qid:36} DIRTY — uncommitted changes, skipped. Commit/stash first.  {title}")
        elif r.get("already_up_to_date"):
            print(f"  ✅ {qid:36} already up to date (behind=0).  {title}")
        elif r.get("merged"):
            auto_resolved = r.get("auto_resolved_foreign_ledger_files") or []
            foreign_note = f"  [auto-resolved {len(auto_resolved)} foreign ledger conflict(s) -> {base_branch}]" if auto_resolved else ""
            print(f"  🔄 {qid:36} merged: {r.get('before_behind')} -> {r.get('after_behind')} behind.{foreign_note}  {title}")
        elif r.get("conflict"):
            sample = ", ".join(r.get("conflict_files", [])[:5]) or "unknown files"
            print(f"  🔴 {qid:36} CONFLICT in [{sample}] — aborted, needs manual resolution.  {title}")
        else:
            print(f"  ❓ {qid:36} {r.get('error', 'unknown result')}  {title}")
    print("=" * 78)


def cmd_diff(args):
    court_root = store.get_court_root()
    repo_root = court_root.parent
    try:
        quest = store.load(args.quest_id, court_root=court_root)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    wt = None
    if quest.worktree and Path(quest.worktree).is_dir():
        wt = Path(quest.worktree)
    else:
        wt = git_ops.find_worktree_for_quest(quest, cwd=repo_root)

    if not wt or not wt.is_dir():
        print(f"ERROR: No active worktree found for {quest.id} (branch: {quest.branch or '-'})", file=sys.stderr)
        sys.exit(1)

    base = getattr(args, "base", "castle") or "castle"
    diff_res = git_ops.get_worktree_diff(
        wt,
        base=base,
        stat_only=args.stat,
        file_stats=True,
    )

    if not diff_res.get("ok"):
        print(f"ERROR: {diff_res.get('error')}", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "json", False):
        print(json.dumps(diff_res, indent=2))
        return

    print("=" * 76)
    print(f"WORKTREE DIFF: {quest.id} ({wt.name}) vs {base}")
    print(f"Branch: {quest.branch} | Files Changed: {diff_res['total_files']} (+{diff_res['total_additions']}, -{diff_res['total_deletions']})")
    print("=" * 76)

    if args.numstat:
        for f in diff_res["files"]:
            bin_tag = " (binary)" if f["binary"] else ""
            print(f"{f['additions']:>6}\t{f['deletions']:>6}\t{f['path']}{bin_tag}")
    elif args.stat or not (args.patch or getattr(args, "full", False)):
        if diff_res.get("stat"):
            print(diff_res["stat"])
        else:
            print("(no diff vs base branch)")
    if args.patch or getattr(args, "full", False):
        if diff_res.get("patch"):
            print("\n" + diff_res["patch"])
        else:
            print("(no diff patch vs base branch)")


def cmd_worktree_doc(args):
    court_root = store.get_court_root()
    repo_root = court_root.parent
    quest = store.load(args.quest_id, court_root=court_root)
    wt = git_ops.find_worktree_for_quest(quest, cwd=repo_root)
    if not wt or not wt.is_dir():
        print(f"ERROR: No active worktree found for {quest.id} (branch: {quest.branch or '-'})", file=sys.stderr)
        sys.exit(1)

    candidate_paths = [
        wt / ".court" / "quests" / f"{quest.id}.md",
        wt / ".court" / "epics" / f"{quest.id}.md",
    ]
    short_num = quest.id.split("-")[0].lstrip("Qq")
    prefix = f"Q{short_num}-"
    for d in (wt / ".court" / "quests", wt / ".court" / "epics"):
        if d.is_dir():
            candidate_paths.extend(list(d.glob(f"{prefix}*.md")))

    src_file = None
    for cp in candidate_paths:
        if cp.exists() and cp.is_file():
            src_file = cp
            break

    if not src_file:
        print(f"ERROR: No quest markdown found inside worktree {wt}", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "path", False):
        print(src_file)
        return

    wt_content = src_file.read_text(encoding="utf-8")
    castle_path = store.find_path(quest.id, court_root=court_root)
    castle_content = castle_path.read_text(encoding="utf-8") if castle_path and castle_path.exists() else ""

    if getattr(args, "json", False):
        print(json.dumps({
            "quest_id": quest.id,
            "worktree_path": str(wt),
            "document_path": str(src_file),
            "content": wt_content,
            "modified_from_castle": wt_content != castle_content,
        }, indent=2))
        return

    if getattr(args, "diff", False):
        diff = list(difflib.unified_diff(
            castle_content.splitlines(keepends=True),
            wt_content.splitlines(keepends=True),
            fromfile=f"castle/{castle_path.name if castle_path else quest.id + '.md'}",
            tofile=f"worktree/{src_file.name}",
        ))
        if diff:
            print("".join(diff))
        else:
            print("No difference between castle and worktree quest documents.")
        return

    if getattr(args, "sync", False):
        success, msg = ward.sync_tribute_from_worktree(quest, worktree_path=wt, court_root=court_root)
        print(f"Sync result for {quest.id}: {msg}")
        return

    print("=" * 76)
    print(f"WORKTREE TASK DOCUMENT: {quest.id} ({src_file})")
    print("=" * 76)
    print(wt_content)


def cmd_fix_branches(args):
    repo_root = Path.cwd()
    if getattr(args, "current_worktree", False):
        res = branch_ops.realign_current_worktree(
            cwd=repo_root,
            sync_castle=getattr(args, "sync", True),
        )
        if getattr(args, "json", False):
            print(json.dumps(res, indent=2))
        else:
            if res.get("renamed"):
                print(f"Realigned branch: {res['previous_branch']} -> {res['canonical_branch']} ({res.get('reason')})")
            else:
                print(f"Current branch is already canonical: {res['canonical_branch']}")
            if res.get("sync_castle") is True:
                print("Fast-forward synced with castle (behind: 0).")
            elif res.get("sync_castle") is False:
                print(f"WARNING: fast-forward sync with castle failed: {res.get('sync_error')}")
        if not res.get("ok"):
            sys.exit(1)
        return

    dry_run = getattr(args, "dry_run", False)
    res = branch_ops.realign_all_branches(dry_run=dry_run, cwd=repo_root)

    if getattr(args, "json", False):
        print(json.dumps(res, indent=2))
        return

    prefix = "[DRY RUN] " if dry_run else ""
    print("=" * 76)
    print(f"{prefix}THE COURT BRANCH REALIGNMENT — FOLDER HIERARCHY AUDIT & REPAIR")
    print("=" * 76)
    print(f"Scanned {res['total_scanned']} local branches. Identified {res['realigned_count']} flat branches to realign.\n")

    if res.get("actions"):
        for a in res["actions"]:
            action = a["action_taken"]
            checked = f" (worktree: {a['worktree_path']})" if a["checked_out"] else " (loose branch)"
            print(f"  • {a['old_branch']} -> {a['new_branch']}{checked}")
            print(f"    Action: {action} | Source: {a['reason']}")

    if res.get("errors"):
        print("\nErrors encountered during realignment:")
        for err in res["errors"]:
            print(f"  - {err['old_branch']} -> {err['new_branch']}: {err.get('error')}")

    if not res.get("actions") and not res.get("errors"):
        print("All local branches and worktrees are already using canonical forward-slash folder hierarchy.")

    print("\n" + "=" * 76)
    if not res.get("ok"):
        sys.exit(1)


def _ward_dir(court_root: Optional[Path] = None) -> Path:
    root = court_root or store.get_court_root()
    return root / "ward"


def _read_last_ward_survey(court_root: Optional[Path] = None) -> str:
    """Parse the last recorded patrol timestamp from the durable Ward log
    (`.court/ward/WARDENS_LOG.md`), if one exists."""
    log_path = _ward_dir(court_root) / "WARDENS_LOG.md"
    if not log_path.exists():
        return ""
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip().lower().startswith("last survey:"):
            val = line.split(":", 1)[1].strip()
            if val and val.lower() not in ("(none yet)", "none", "-"):
                return val
    return ""


def _list_pending_ward_reports(court_root: Optional[Path] = None) -> list[Path]:
    """List standalone Warden Reports on disk not yet chartered into a Quest."""
    reports_dir = _ward_dir(court_root) / "reports"
    if not reports_dir.is_dir():
        return []
    return sorted(reports_dir.glob("*.md"))


def _resolve_ward_harness_cmd(court_root: Optional[Path] = None) -> Optional[list[str]]:
    """Resolve an optional, project-configurable command used to check for
    fresh unresolved issues from whatever error-monitoring system this repo
    uses. Fully decoupled from the Court engine, which has zero built-in
    knowledge of any specific vendor's API: configure a harness via the
    `COURT_WARD_HARNESS_CMD` environment variable (a shell command string),
    or by dropping a `.court/ward_harness.py` script into the repo. Either
    must accept `--check --env <name> --limit <n>` and print a JSON object
    `{"fresh_issue_count": int, "fresh_issues": [...]}` to stdout.
    """
    env_cmd = os.environ.get("COURT_WARD_HARNESS_CMD")
    if env_cmd:
        return shlex.split(env_cmd)
    root = court_root or store.get_court_root()
    script = root / "ward_harness.py"
    if script.is_file():
        return [sys.executable, str(script)]
    return None


def _check_fresh_issues(
    env: str = "production",
    limit: int = 5,
    court_root: Optional[Path] = None,
    timeout: int = 15,
) -> dict:
    """Best-effort, vendor-agnostic call into a project-configured harness.
    Any failure (missing script, missing credentials, network error, timeout)
    degrades gracefully to `{"available": False, ...}` — this never raises."""
    cmd = _resolve_ward_harness_cmd(court_root)
    if not cmd:
        return {
            "available": False,
            "reason": "no ward harness configured (optional; set COURT_WARD_HARNESS_CMD or add .court/ward_harness.py)",
        }
    try:
        proc = subprocess.run(
            cmd + ["--check", "--env", env, "--limit", str(limit)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            reason = (proc.stderr or proc.stdout or "harness exited non-zero").strip()[:300]
            return {"available": False, "reason": reason}
        data = json.loads(proc.stdout.strip() or "{}")
        data["available"] = True
        return data
    except Exception as e:
        return {"available": False, "reason": str(e)}


def cmd_ward(args):
    court_root = store.get_court_root()
    last_survey = _read_last_ward_survey(court_root)
    pending_reports = _list_pending_ward_reports(court_root)
    realm = ward.audit_realm(base_branch=getattr(args, "base", "castle") or "castle", court_root=court_root)
    non_compliant = [a for a in realm["audits"] if not a.is_compliant]

    fresh = None
    if getattr(args, "check_fresh", False):
        fresh = _check_fresh_issues(env=args.env, limit=args.limit, court_root=court_root)

    if getattr(args, "json", False):
        out = {
            "last_survey": last_survey or None,
            "pending_ward_reports": [str(p) for p in pending_reports],
            "realm_compliance": {
                "total_active": realm["total_active"],
                "compliant_count": realm["compliant_count"],
                "non_compliant_count": realm["non_compliant_count"],
                "dirty_count": realm["dirty_count"],
                "behind_count": realm["behind_count"],
                "non_compliant_ids": [a.quest_id for a in non_compliant],
            },
            "fresh_issues": fresh,
        }
        print(json.dumps(out, indent=2))
        return

    print("=" * 76)
    print("THE WARD — COMPLIANCE PATROL & REALM HEALTH")
    print("=" * 76)
    print(f"Last Survey: {last_survey or '(never surveyed — no patrol has run yet)'}")
    print()

    print(f"WARD REPORTS AWAITING CHARTER ({len(pending_reports)})")
    if pending_reports:
        for p in pending_reports:
            print(f"  - {p.name}")
    else:
        print("  (none pending)")
    print()

    print(f"REALM COMPLIANCE HEALTH: {realm['compliant_count']}/{realm['total_active']} active Quests compliant")
    print(f"  Dirty worktrees: {realm['dirty_count']} | Behind base: {realm['behind_count']}")
    if non_compliant:
        for a in non_compliant:
            print(f"  [NON-COMPLIANT] {a.quest_id}: {a.title}")
            for v in a.violations:
                print(f"      - {v}")
    print()

    if fresh is not None:
        print("FRESH ISSUE DETECTION (optional, project-configured harness)")
        if fresh.get("available"):
            count = fresh.get("fresh_issue_count", 0)
            print(f"  {count} new unresolved issue(s) detected since last ingest.")
            for issue in fresh.get("fresh_issues", [])[:10]:
                print(f"  - [{issue.get('short_id', '?')}] {issue.get('title', 'Unknown')}")
        else:
            print(f"  (unavailable: {fresh.get('reason', 'unknown')})")
        print()

    print("Fresh-issue detection is optional. To enable it, configure a harness:")
    print("  export COURT_WARD_HARNESS_CMD='python3 path/to/your_harness.py'")
    print("  court ward --check-fresh --env production")
    print("=" * 76)


def build_parser():
    p = argparse.ArgumentParser(
        prog="court",
        description="Kilo Castle / The Court: Deterministic Multi-Agent Orchestration Framework",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # init
    p_init = sub.add_parser("init", help="Initialize Court in current repository")
    p_init.add_argument("target_dir", nargs="?", default=None, help="Target repository directory (defaults to current dir)")
    p_init.add_argument("--force", "-f", action="store_true", help="Overwrite existing templates/commands")
    p_init.set_defaults(func=cmd_init)

    # new
    p_new = sub.add_parser("new", help="Create a new Quest or Epic")
    p_new.add_argument("--app", required=True, help="App / component domain (e.g. platform, billing, core)")
    p_new.add_argument("--concern", required=True, help="Short slug for the concern")
    p_new.add_argument("--title", required=True, help="Concise descriptive title")
    p_new.add_argument("--section", choices=SECTIONS, default="")
    p_new.add_argument("--tags", default="", help="Tags matching section/category")
    p_new.add_argument("--branch", default="", help="Branch name override (defaults to tree format)")
    p_new.add_argument("--kind", choices=KINDS, default="quest")
    p_new.add_argument("--epic", default="", help="Parent Epic Quest id, if any")
    p_new.add_argument("--goal", default="", help="Goal & Scope body text")
    p_new.add_argument("--tribute", default="", help="Expected Tribute body text")
    p_new.set_defaults(func=cmd_new)

    # show
    p_show = sub.add_parser("show", help="Print a Quest/Epic's full markdown")
    p_show.add_argument("quest_id")
    p_show.set_defaults(func=cmd_show)

    # list
    p_list = sub.add_parser("list", help="List Quests/Epics")
    p_list.add_argument("--status", choices=STATUSES, default=None)
    p_list.add_argument("--app", default=None)
    p_list.add_argument("--all", action="store_true", help="Include archived quests")
    p_list.set_defaults(func=cmd_list)

    # status
    p_status = sub.add_parser("status", help="Full court dashboard")
    p_status.add_argument("--tree", action="store_true", help="Display hierarchical quest tree dashboard")
    p_status.set_defaults(func=cmd_status)

    # tree
    p_tree = sub.add_parser("tree", help="Display hierarchical Quest and Epic tree")
    p_tree.add_argument("--all", action="store_true", help="include archived Quests and Epics")
    p_tree.set_defaults(func=cmd_tree)

    # advance
    p_advance = sub.add_parser("advance", help="Change a Quest's pipeline stage")
    p_advance.add_argument("quest_id")
    p_advance.add_argument("status", choices=STATUSES)
    p_advance.add_argument("--note", default="")
    p_advance.set_defaults(func=cmd_advance)

    # log
    p_log = sub.add_parser("log", help="Append a history note without changing status")
    p_log.add_argument("quest_id")
    p_log.add_argument("note")
    p_log.set_defaults(func=cmd_log)

    # set-field
    p_field = sub.add_parser("set-field", help="Set a frontmatter field")
    p_field.add_argument("quest_id")
    p_field.add_argument("field")
    p_field.add_argument("value")
    p_field.set_defaults(func=cmd_set_field)

    # set-section
    p_section = sub.add_parser("set-section", help="Replace/append a body section")
    p_section.add_argument("quest_id")
    p_section.add_argument("section")
    p_section.add_argument("--content", default=None)
    p_section.add_argument("--file", default=None)
    p_section.add_argument("--append", action="store_true")
    p_section.set_defaults(func=cmd_set_section)

    # verify
    p_verify = sub.add_parser("verify", help="Run a deterministic worktree/test check")
    p_verify.add_argument("quest_id")
    p_verify.add_argument("--test-cmd", default=None)
    p_verify.add_argument("--timeout", type=int, default=600)
    p_verify.set_defaults(func=cmd_verify)

    # verify-merged
    p_verify_merged = sub.add_parser("verify-merged", help="Verify if Quest branch is merged into castle")
    p_verify_merged.add_argument("quest_id")
    p_verify_merged.add_argument("--sync", action="store_true", help="Fast-forward worktree to castle if merged")
    p_verify_merged.set_defaults(func=cmd_verify_merged)

    # raze
    p_raze = sub.add_parser("raze", help="Raze a Quest: verify merge, sync diffs to castle (ahead: 0, behind: 0), queue for Ashes")
    p_raze.add_argument("quest_id", help="Quest ID or 'all'")
    p_raze.add_argument("--archive-pruned", action="store_true", default=True, help="Auto-archive already-pruned quests")
    p_raze.set_defaults(func=cmd_raze)

    # teardown-list
    p_teardown = sub.add_parser("teardown-list", help="List worktrees ready for M'Lord to prune")
    p_teardown.set_defaults(func=cmd_teardown_list)

    # archive
    p_archive = sub.add_parser("archive", help="Move a Quest/Epic file into .court/archive/")
    p_archive.add_argument("quest_id")
    p_archive.set_defaults(func=cmd_archive)

    # rollup
    p_rollup = sub.add_parser("rollup", help="Extract and roll up specific tribute sections across Quests")
    p_rollup.add_argument("--section", required=True, choices=["ballad", "tribute", "tally", "penance", "audience", "opinion"], help="Tribute subsection to extract")
    p_rollup.add_argument("--app", default=None, help="Filter by app domain")
    p_rollup.add_argument("--epic", default=None, help="Filter by parent Epic ID")
    p_rollup.add_argument("--status", default=None, help="Filter by status (comma-separated)")
    p_rollup.add_argument("--all", action="store_true", help="Include archived Quests")
    p_rollup.set_defaults(func=cmd_rollup)

    # tally
    p_tally = sub.add_parser("tally", help="Extract and summarize production verification runbooks and UI paths across Quests (/tally)")
    p_tally.add_argument("--app", default=None, help="Filter by app domain")
    p_tally.add_argument("--epic", default=None, help="Filter by parent Epic ID")
    p_tally.add_argument("--status", default=None, help="Filter by status (comma-separated)")
    p_tally.add_argument("--all", action="store_true", help="Include archived Quests")
    p_tally.set_defaults(func=cmd_tally)

    # edict
    p_edict = sub.add_parser("edict", help="View or add royal edicts and strategic priorities")
    p_edict.add_argument("content", nargs="?", default=None, help="Edict text to record")
    p_edict.add_argument("--file", default=None, help="Read edict from file")
    p_edict.add_argument("--append", action="store_true", default=True, help="Append to existing edicts")
    p_edict.set_defaults(func=cmd_edict)

    # ship
    p_ship = sub.add_parser("ship", help="Cog Ship: generate the full deployment convoy summary (/bard, /coffers, /atone, /murmur)")
    p_ship.add_argument("--app", default=None, help="Filter by app domain")
    p_ship.add_argument("--epic", default=None, help="Filter by parent Epic ID")
    p_ship.add_argument("--status", default=None, help="Filter by status (default: READY_FOR_TEARDOWN,DONE)")
    p_ship.add_argument("--base", default="main", help="Target production branch (default: main)")
    p_ship.add_argument("--head", default="castle", help="Source staging branch (default: castle)")
    p_ship.add_argument("--all", action="store_true", help="Include archived Quests")
    p_ship.set_defaults(func=cmd_ship)

    # pillory (alias: punish)
    p_pillory = sub.add_parser(
        "pillory",
        aliases=["punish"],
        help="Send a Quest to the pillory: freeze it as PUNISHED with Decrees for a chartered successor (unless proof of landing is found)",
    )
    p_pillory.add_argument("quest_id")
    p_pillory.add_argument("--reason", default="", help="Why the audit was rejected")
    p_pillory.add_argument("--decrees", default="", help="What the successor Quest must keep/discard/reuse")
    p_pillory.add_argument("--successor", default="", help="Successor Quest id, if already chartered")
    p_pillory.set_defaults(func=cmd_pillory)

    # stamp
    p_stamp = sub.add_parser("stamp", help="Stamp Quests onto a Cog Ship convoy id (allocates next cogship-NNN unless --cogship given)")
    p_stamp.add_argument("quest_ids", help="Comma-separated Quest IDs (e.g. Q101,Q102,Q105)")
    p_stamp.add_argument("--cogship", default=None, help="Existing Cog Ship ID to stamp (e.g. cogship-002); omit or pass 'new' to allocate the next id")
    p_stamp.add_argument("--commit", action="store_true", help="Auto-commit the stamped quest files (see `court` docs on auto-commit safety)")
    p_stamp.set_defaults(func=cmd_stamp)

    # audit
    p_audit = sub.add_parser("audit", help="Audit Quest tribute presence, gaps, and protocol compliance")
    p_audit.add_argument("quest_id", nargs="?", default=None, help="Optional specific Quest ID to audit")
    p_audit.add_argument("--app", default=None, help="Filter by app domain")
    p_audit.add_argument("--epic", default=None, help="Filter by parent Epic ID")
    p_audit.add_argument("--status", default=None, help="Filter by status (comma-separated)")
    p_audit.add_argument("--base", default="castle", help="Base branch to check drift against (default: castle)")
    p_audit.add_argument("--all", action="store_true", help="Include archived Quests")
    p_audit.add_argument("--json", action="store_true", help="Output JSON format")
    p_audit.set_defaults(func=cmd_audit)

    # levy
    p_levy = sub.add_parser("levy", help="Deterministic worktree triage, tribute synchronization, and review advancement")
    p_levy.add_argument("quest_id", nargs="?", default=None, help="Optional specific Quest ID to levy")
    p_levy.add_argument("--app", default=None, help="Filter by app domain")
    p_levy.add_argument("--epic", default=None, help="Filter by parent Epic ID")
    p_levy.add_argument("--status", default=None, help="Filter by status (default: WORKING,DISPATCHED)")
    p_levy.add_argument("--base", default="castle", help="Base branch to check drift against (default: castle)")
    p_levy.add_argument("--advance", action="store_true", help="Auto-advance compliant Quests to REVIEW")
    p_levy.add_argument("--no-sync", dest="sync", action="store_false", default=True, help="Skip syncing tribute from worktree")
    p_levy.add_argument("--no-rebase", dest="rebase", action="store_false", default=True, help="Skip the mechanical pre-flight `git merge <base>` sweep (see `court rebase --help`)")
    p_levy.add_argument("--all", action="store_true", help="Include all Quests regardless of status")
    p_levy.add_argument("--json", action="store_true", help="Output JSON format")
    p_levy.set_defaults(func=cmd_levy)

    # rebase
    p_rebase = sub.add_parser("rebase", help="Mechanically converge worktrees onto --base (default castle) via `git merge` — zero agent turns")
    p_rebase.add_argument("quest_id", nargs="?", default=None, help="Optional specific Quest ID to rebase")
    p_rebase.add_argument("--app", default=None, help="Filter by app domain")
    p_rebase.add_argument("--epic", default=None, help="Filter by parent Epic ID")
    p_rebase.add_argument("--status", default=None, help="Filter by status (default: WORKING,REVIEW,DISPATCHED,GATE)")
    p_rebase.add_argument("--base", default="castle", help="Base branch to merge into each worktree (default: castle)")
    p_rebase.add_argument("--dry-run", dest="dry_run", action="store_true", help="Report current ahead/behind only; do not merge")
    p_rebase.add_argument("--all", action="store_true", help="Include archived Quests")
    p_rebase.add_argument("--json", action="store_true", help="Output JSON format")
    p_rebase.set_defaults(func=cmd_rebase)

    # diff
    p_diff = sub.add_parser("diff", help="Display structured worktree diff and file statistics vs base")
    p_diff.add_argument("quest_id", help="Quest ID to diff")
    p_diff.add_argument("--base", default="castle", help="Base branch/ref to diff against (default: castle)")
    p_diff.add_argument("--stat", action="store_true", help="Show diffstat summary only")
    p_diff.add_argument("--numstat", action="store_true", help="Show per-file additions/deletions")
    p_diff.add_argument("--patch", "--full", dest="patch", action="store_true", help="Show full patch")
    p_diff.add_argument("--json", action="store_true", help="Output JSON format")
    p_diff.set_defaults(func=cmd_diff)

    # worktree-doc
    p_wt_doc = sub.add_parser("worktree-doc", help="Inspect, diff, or sync the task document from a Quest's active worktree")
    p_wt_doc.add_argument("quest_id", help="Quest ID (e.g. Q079)")
    p_wt_doc.add_argument("--path", action="store_true", help="Print absolute path of worktree task document")
    p_wt_doc.add_argument("--diff", action="store_true", help="Show diff between the canonical ledger doc and the worktree doc")
    p_wt_doc.add_argument("--sync", action="store_true", help="Sync tribute and frontmatter from worktree doc to the canonical ledger")
    p_wt_doc.add_argument("--json", action="store_true", help="Output JSON format")
    p_wt_doc.set_defaults(func=cmd_worktree_doc)

    # timber
    p_timber = sub.add_parser("timber", help="List physical git worktrees mapped to Agent Manager sections and Court Quests")
    p_timber.add_argument("--json", action="store_true", help="Output JSON format")
    p_timber.set_defaults(func=cmd_timber)

    # ward
    p_ward = sub.add_parser("ward", help="Compliance patrol: last survey, pending Warden Reports, and realm-wide compliance health")
    p_ward.add_argument("--base", default="castle", help="Base branch to check drift against (default: castle)")
    p_ward.add_argument("--check-fresh", action="store_true", dest="check_fresh", help="Also call an optional project-configured harness to detect fresh unresolved issues (see COURT_WARD_HARNESS_CMD)")
    p_ward.add_argument("--env", default="production", help="Environment name passed through to the harness when --check-fresh is used")
    p_ward.add_argument("--limit", type=int, default=5, help="Max fresh issues to report when --check-fresh is used")
    p_ward.add_argument("--json", action="store_true", help="Output JSON format")
    p_ward.set_defaults(func=cmd_ward)

    # fix-branches (alias: realign-branches)
    for cmd_alias in ("fix-branches", "realign-branches"):
        p_fix = sub.add_parser(cmd_alias, help="Scan and repair flat branches to forward-slash hierarchy")
        p_fix.add_argument("--dry-run", action="store_true", help="Preview branch renames without executing")
        p_fix.add_argument("--current-worktree", action="store_true", help="Realign branch in the current worktree directory")
        p_fix.add_argument("--no-sync", dest="sync", action="store_false", default=True, help="Skip syncing baseline with castle")
        p_fix.add_argument("--json", action="store_true", help="Output JSON format")
        p_fix.set_defaults(func=cmd_fix_branches)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
