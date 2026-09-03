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
    court edict "Focus on QStash reliability and reduce Neon compute hours"
    court ship --epic Q012
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from court import git_ops, store
from court.models import KINDS, SECTIONS, STATUSES, Quest, now_iso


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


def cmd_teardown_list(args):
    quests = [q for q in store.list_all() if q.status == "READY_FOR_TEARDOWN"]
    if not quests:
        print("(nothing queued for teardown)")
        return
    print("Worktrees ready for M'Lord to manually prune in Agent Manager:")
    for q in quests:
        print(f"  - {q.id}: branch={q.branch or '-'} worktree={q.worktree or '-'}")


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

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
