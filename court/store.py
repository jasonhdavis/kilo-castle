"""
Filesystem operations for Quest/Epic ledger state under .court/.

Directory layout:
    .court/quests/     active + completed Quests (Q001-App-Concern.md +
                       Q001-App-Concern.events.jsonl)
    .court/epics/      Epic Quests (Q0NN-App-Concern.md, kind=epic)
    .court/archive/    Quests moved out of the active ledger (still readable)
    .court/templates/  prompt templates the Steward fills in when dispatching
    .court/EDICTS.md   Royal decrees and strategic priorities

Storage model: `<id>.md` is a derived, fully regenerated view. The append-only
`<id>.events.jsonl` next to it is the actual source of truth; `save()`/`load()`/
`list_all()` read/write through `eventlog.py`, folding the event log into a
`Quest` on every read and appending only the fields/sections that changed on
every write, instead of rewriting the whole record in place.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

from .models import Quest, now_iso
from . import git_ops
from . import eventlog

_ID_NUM_RE = re.compile(r"^Q(\d+)-")
_COGSHIP_NUM_RE = re.compile(r"cogship[-_]?(\d+)", re.IGNORECASE)
_COGSHIP_FM_RE = re.compile(r"^cogship_id:\s*(\S.*)$", re.MULTILINE)

_current_branch_cache: Optional[str] = None


def get_court_root(start_path: Optional[Path | str] = None) -> Path:
    """Locate the .court root directory by searching upwards from start_path or cwd,
    or reading COURT_DIR from environment."""
    env_dir = os.environ.get("COURT_DIR")
    if env_dir:
        return Path(env_dir).resolve()

    current = (Path(start_path) if start_path else Path.cwd()).resolve()
    for parent in [current, *current.parents]:
        court_candidate = parent / ".court"
        if court_candidate.is_dir():
            return court_candidate
        git_candidate = parent / ".git"
        if git_candidate.exists():
            return parent / ".court"

    return current / ".court"


def get_quests_dir(court_root: Optional[Path] = None) -> Path:
    return (court_root or get_court_root()) / "quests"


def get_epics_dir(court_root: Optional[Path] = None) -> Path:
    return (court_root or get_court_root()) / "epics"


def get_archive_dir(court_root: Optional[Path] = None) -> Path:
    return (court_root or get_court_root()) / "archive"


def get_templates_dir(court_root: Optional[Path] = None) -> Path:
    return (court_root or get_court_root()) / "templates"


def get_edicts_path(court_root: Optional[Path] = None) -> Path:
    return (court_root or get_court_root()) / "EDICTS.md"


def all_state_dirs(court_root: Optional[Path] = None) -> Iterable[Path]:
    root = court_root or get_court_root()
    for d in (get_quests_dir(root), get_epics_dir(root), get_archive_dir(root)):
        d.mkdir(parents=True, exist_ok=True)
        yield d


def _current_repo_branch(repo_root: Optional[Path] = None) -> str:
    """Branch checked out at repo_root (the current worktree or base trunk)."""
    global _current_branch_cache
    if repo_root is None:
        if _current_branch_cache is None:
            root = get_court_root().parent
            res = git_ops._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
            _current_branch_cache = res.get("stdout", "").strip() if res.get("ok") else ""
        return _current_branch_cache
    res = git_ops._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    return res.get("stdout", "").strip() if res.get("ok") else ""


def _commit_allowed_here(quest: Quest, court_root: Optional[Path] = None) -> bool:
    """Guard against cross-branch pollution: auto-committing a Quest's file is
    only safe when the current checkout is the base trunk (castle/main) or that
    exact Quest's own registered branch."""
    root = court_root or get_court_root()
    repo_dir = root.parent
    git_check = git_ops._run(["git", "rev-parse", "--is-inside-work-tree"], repo_dir)
    if not git_check.get("ok") or git_check.get("stdout", "").strip() != "true":
        return True
    current = _current_repo_branch(repo_dir)
    if not current:
        return True
    if current in ("castle", "main"):
        return True
    return bool(quest.branch) and current == quest.branch


def normalize_cogship_id(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = str(value).strip()
    if v.isdigit():
        return f"cogship-{int(v):03d}"
    m = _COGSHIP_NUM_RE.search(v)
    if not m:
        return None
    return f"cogship-{int(m.group(1)):03d}"


def _scan_existing_cogship_numbers(court_root: Optional[Path] = None) -> int:
    highest = 0
    for d in all_state_dirs(court_root):
        for p in d.glob("Q*.md"):
            try:
                text = p.read_text(encoding="utf-8")
                m = _COGSHIP_FM_RE.search(text)
                if m:
                    norm = normalize_cogship_id(m.group(1))
                    if norm:
                        highest = max(highest, int(norm.split("-")[1]))
            except Exception:
                continue
    return highest


def next_cogship_id(court_root: Optional[Path] = None) -> str:
    return f"cogship-{_scan_existing_cogship_numbers(court_root) + 1:03d}"


def stamp_cogship(
    quests: Iterable[Quest],
    cogship_id: Optional[str] = None,
    court_root: Optional[Path] = None,
    auto_commit: bool = True,
    commit_msg: Optional[str] = None,
) -> str:
    """Stamp a cogship_id onto a batch of Quest objects and persist them."""
    root = court_root or get_court_root()
    if cogship_id is None:
        cogship_id = next_cogship_id(root)
    else:
        norm = normalize_cogship_id(cogship_id)
        if norm:
            cogship_id = norm

    archive_d = get_archive_dir(root)
    for q in quests:
        src = find_path(q.id, root)
        if src is None or src.parent == archive_d:
            raise ValueError(f"Cannot stamp {q.id}: not an active quest/epic file")
        prior = q.cogship_id or ""
        q.cogship_id = cogship_id
        q.log_ledger(
            q.status,
            q.status,
            f"Stamped onto {cogship_id}"
            + (f" (reassigned from {prior})" if prior and prior != cogship_id else ""),
        )
        msg = commit_msg or f"court: stamp {q.id} onto {cogship_id}"
        save(q, court_root=root, auto_commit=auto_commit, commit_msg=msg)
    return cogship_id


def next_number(court_root: Optional[Path] = None) -> int:
    """Single global counter shared by Quests and Epics."""
    highest = 0
    for d in all_state_dirs(court_root):
        for p in d.glob("Q*.md"):
            m = _ID_NUM_RE.match(p.stem)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


def slugify_title(app: str, concern: str) -> str:
    parts = re.split(r"[\s_/-]+", f"{app} {concern}".strip())
    parts = [p for p in parts if p]
    return "-".join(p[:1].upper() + p[1:] for p in parts)


def make_id(app: str, concern: str, number: Optional[int] = None, court_root: Optional[Path] = None) -> str:
    n = number if number is not None else next_number(court_root)
    return f"Q{n:03d}-{slugify_title(app, concern)}"


def path_for(quest: Quest, court_root: Optional[Path] = None) -> Path:
    root = court_root or get_court_root()
    base_dir = get_epics_dir(root) if quest.kind == "epic" else get_quests_dir(root)
    return base_dir / f"{quest.id}.md"


def find_path(quest_id: str, court_root: Optional[Path] = None) -> Optional[Path]:
    root = court_root or get_court_root()
    for d in (get_quests_dir(root), get_epics_dir(root), get_archive_dir(root)):
        p = d / f"{quest_id}.md"
        if p.exists():
            return p
    bare = quest_id.lstrip("Qq")
    try:
        num = int(bare)
    except ValueError:
        return None
    prefix = f"Q{num:03d}-"
    for d in (get_quests_dir(root), get_epics_dir(root), get_archive_dir(root)):
        matches = list(d.glob(f"{prefix}*.md"))
        if matches:
            return matches[0]
    return None


def _load_from_path(p: Path) -> Quest:
    """Event-log-first load for an already-resolved quest/epic file path."""
    ev_path = eventlog.events_path_for(p)
    events = eventlog.read_events(ev_path)
    if events:
        kind = "epic" if p.parent.name == "epics" else "quest"
        return eventlog.fold_events(events, p.stem, kind)
    return Quest.from_markdown(p.read_text(encoding="utf-8"))


def save(
    quest: Quest,
    court_root: Optional[Path] = None,
    auto_commit: bool = True,
    commit_msg: Optional[str] = None,
) -> Path:
    root = court_root or get_court_root()
    p = path_for(quest, root)
    if auto_commit and not _commit_allowed_here(quest, court_root=root):
        current = _current_repo_branch(root.parent) or "(unknown/detached)"
        print(
            f"WARNING: refusing to save {quest.id} from branch '{current}' — it is "
            f"neither a protected trunk (castle/main) nor {quest.id}'s "
            f"own branch ({quest.branch or 'unset'}). Re-run this from a protected "
            f"trunk or {quest.id}'s own worktree to save/auto-commit it.",
            file=sys.stderr,
        )
        return p
    p.parent.mkdir(parents=True, exist_ok=True)

    ev_path = eventlog.events_path_for(p)
    prior_events = eventlog.read_events(ev_path)
    prior_quest = eventlog.fold_events(prior_events, quest.id, quest.kind) if prior_events else None
    ts = quest.updated_at or now_iso()
    new_events = eventlog.build_events_for_save(quest, prior_quest, ts)

    paths_to_commit = [p]
    if new_events:
        eventlog.append_events(ev_path, new_events)
        paths_to_commit.append(ev_path)

    p.write_text(quest.to_markdown(), encoding="utf-8")

    if auto_commit:
        msg = commit_msg or f"court: save {quest.id}"
        res = git_ops.git_commit_paths(paths_to_commit, msg, cwd=root.parent)
        if not res.get("ok") and not res.get("no_changes"):
            warning = res.get("warning") or res.get("stderr") or "unknown git error"
            print(f"WARNING: autocommit failed for {p.name}: {warning}", file=sys.stderr)
    return p


def load(quest_id: str, court_root: Optional[Path] = None) -> Quest:
    p = find_path(quest_id, court_root)
    if p is None:
        raise FileNotFoundError(f"No quest/epic file found for id {quest_id!r}")
    return _load_from_path(p)


def list_all(include_archive: bool = False, court_root: Optional[Path] = None) -> list[Quest]:
    root = court_root or get_court_root()
    dirs = [get_quests_dir(root), get_epics_dir(root)]
    if include_archive:
        dirs.append(get_archive_dir(root))
    out = []
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        for p in sorted(d.glob("Q*.md")):
            try:
                out.append(_load_from_path(p))
            except Exception as e:
                print(f"WARNING: failed to parse {p}: {e}")
    return sorted(out, key=lambda q: q.id)


def archive(
    quest_id: str,
    court_root: Optional[Path] = None,
    auto_commit: bool = True,
    commit_msg: Optional[str] = None,
) -> Path:
    root = court_root or get_court_root()
    src = find_path(quest_id, root)
    archive_d = get_archive_dir(root)
    if src is None or src.parent == archive_d:
        raise FileNotFoundError(f"No active quest/epic file found for id {quest_id!r}")
    archive_d.mkdir(parents=True, exist_ok=True)
    dst = archive_d / src.name
    src_events = eventlog.events_path_for(src)
    dst_events = eventlog.events_path_for(dst)
    paths_to_commit = [src, dst]
    has_events = src_events.exists()
    src.rename(dst)
    if has_events:
        src_events.rename(dst_events)
        paths_to_commit.extend([src_events, dst_events])
    if auto_commit:
        msg = commit_msg or f"court: archive {quest_id}"
        res = git_ops.git_commit_paths(paths_to_commit, msg, cwd=root.parent)
        if not res.get("ok") and not res.get("no_changes"):
            warning = res.get("warning") or res.get("stderr") or "unknown git error"
            print(f"WARNING: autocommit failed for archive {quest_id}: {warning}", file=sys.stderr)
    return dst


def rollup(
    section_name: str,
    app: Optional[str] = None,
    epic: Optional[str] = None,
    status: Optional[str] = None,
    cogship: Optional[str] = None,
    include_archive: bool = False,
    court_root: Optional[Path] = None,
    quests: Optional[list[Quest]] = None,
) -> list[tuple[Quest, str]]:
    """Roll up a specific tribute subsection across Quests matching filters or given list."""
    if quests is None:
        quests = list_all(include_archive=include_archive, court_root=court_root)
        if app:
            quests = [q for q in quests if q.app.lower() == app.lower()]
        if epic:
            epic_norm = epic.lower().lstrip("q").partition("-")[0]
            quests = [
                q for q in quests
                if q.parent_epic.lower().lstrip("q").partition("-")[0] == epic_norm
                or q.id.lower().lstrip("q").partition("-")[0] == epic_norm
            ]
        if status:
            status_set = {s.strip().upper() for s in status.split(",")}
            quests = [q for q in quests if q.status in status_set]
        if cogship:
            cog_norm = normalize_cogship_id(cogship)
            quests = [q for q in quests if normalize_cogship_id(q.cogship_id) == cog_norm]

    results = []
    for q in quests:
        val = q.extract_tribute_subsection(section_name)
        if val:
            results.append((q, val))
    return results


rollup_section = rollup


def get_hierarchy(include_archive: bool = False, court_root: Optional[Path] = None) -> dict:
    """Returns a structured hierarchy:
    - epics: list of (epic_quest, list_of_child_quests)
    - standalone: list of quests without parent_epic (not epic, not scout/investigation)
    - scouts: list of scout/investigation quests
    """
    quests = list_all(include_archive=include_archive, court_root=court_root)
    epics = [q for q in quests if q.kind == "epic"]
    scouts = [q for q in quests if q.kind == "scout" or q.section == "Investigation"]

    epic_children: dict[str, list[Quest]] = {e.id: [] for e in epics}
    standalone = []

    for q in quests:
        if q.kind == "epic":
            continue
        if q.parent_epic:
            matched_epic = None
            for e_id in epic_children:
                if e_id == q.parent_epic or e_id.startswith(q.parent_epic) or q.parent_epic.startswith(e_id):
                    matched_epic = e_id
                    break
            if matched_epic:
                epic_children[matched_epic].append(q)
            else:
                standalone.append(q)
        else:
            if q.kind != "scout" and q.section != "Investigation":
                standalone.append(q)

    return {
        "epics": [(e, epic_children[e.id]) for e in epics],
        "standalone": standalone,
        "scouts": scouts,
    }


def get_epic_children(epic_id: str, include_archive: bool = True, court_root: Optional[Path] = None) -> list[Quest]:
    """Return the child Quests/Scouts belonging to the given Epic id."""
    hierarchy = get_hierarchy(include_archive=include_archive, court_root=court_root)
    epic_short = epic_id.strip().lower().lstrip("q").partition("-")[0]
    for epic_quest, children in hierarchy["epics"]:
        epic_quest_short = epic_quest.id.lower().lstrip("q").partition("-")[0]
        if epic_quest.id == epic_id or epic_quest_short == epic_short:
            return children
    return []


def render_ship_manifest_markdown(manifest: dict, epic_id: str = "") -> str:
    """Render an aggregated rollup_ship_manifest() dict as markdown text."""
    quest_count = len(manifest.get("quests", []))
    pillars = [
        ("ballad", "Ballad", manifest.get("ballads", [])),
        ("tribute", "Tribute", manifest.get("tributes", [])),
        ("tally", "Tally", manifest.get("tallies", [])),
        ("penance", "Penance", manifest.get("penances", [])),
        ("opinion", "Opinion", manifest.get("opinions", [])),
        ("commutation", "Commutation", manifest.get("commutations", [])),
    ]

    lines: list[str] = []
    scope = f" of {epic_id}" if epic_id else ""
    lines.append(
        f"_Aggregated automatically from {quest_count} completed child Quest(s){scope} "
        f"(no code changes of its own; this Epic closed directly to Closing Vault)._"
    )

    for key, label, items in pillars:
        lines.append(f"\n## {label}\n")
        if not items:
            lines.append(f"(no {key} content found among child Quests)")
            continue
        for q, content in items:
            lines.append(f"### {q.id}: {q.title}\n")
            lines.append(content.strip())
            lines.append("")

    done = manifest.get("commutations_done", [])
    if done:
        lines.append("\n## Commutations Done\n")
        lines.append(f"({', '.join(q.id for q, _ in done)}) — already executed and logged in Cogship Log\n")

    return "\n".join(lines).strip() + "\n"


def load_edicts(court_root: Optional[Path] = None) -> str:
    p = get_edicts_path(court_root)
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return ""


def save_edicts(
    content: str,
    court_root: Optional[Path] = None,
    auto_commit: bool = True,
    commit_msg: Optional[str] = None,
) -> Path:
    p = get_edicts_path(court_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content.strip() + "\n", encoding="utf-8")
    if auto_commit:
        msg = commit_msg or "court: update royal edicts"
        root = court_root or get_court_root()
        res = git_ops.git_commit_paths([p], msg, cwd=root.parent)
        if not res.get("ok") and not res.get("no_changes"):
            warning = res.get("warning") or res.get("stderr") or "unknown git error"
            print(f"WARNING: autocommit failed for {p.name}: {warning}", file=sys.stderr)
    return p


def rollup_ship_manifest(
    app: Optional[str] = None,
    epic: Optional[str] = None,
    status: Optional[str] = None,
    cogship: Optional[str] = None,
    include_archive: bool = False,
    court_root: Optional[Path] = None,
    quests: Optional[list[Quest]] = None,
) -> dict:
    """Extract and aggregate all rollups (ballad, tribute, tally, penance, opinion, commutation)
    for Quests in the deployment convoy.
    Defaults to Quests with status READY_TO_RAZE, READY_FOR_TEARDOWN, or DONE if status is not specified.
    """
    if quests is None:
        if status is None:
            target_statuses = {"READY_TO_RAZE", "READY_FOR_TEARDOWN", "DONE"}
        else:
            target_statuses = {s.strip().upper() for s in status.split(",")}

        all_quests = list_all(include_archive=include_archive, court_root=court_root)
        if app:
            all_quests = [q for q in all_quests if q.app.lower() == app.lower()]
        if epic:
            epic_norm = epic.lower().lstrip("q").partition("-")[0]
            all_quests = [
                q for q in all_quests
                if q.parent_epic.lower().lstrip("q").partition("-")[0] == epic_norm
                or q.id.lower().lstrip("q").partition("-")[0] == epic_norm
            ]
        if cogship:
            cog_norm = normalize_cogship_id(cogship)
            all_quests = [q for q in all_quests if normalize_cogship_id(q.cogship_id) == cog_norm]

        convoy_quests = [q for q in all_quests if q.status in target_statuses]
    else:
        convoy_quests = list(quests)
        if status:
            target_statuses = {s.strip().upper() for s in status.split(",")}
            convoy_quests = [q for q in convoy_quests if q.status in target_statuses]

    manifest = {
        "quests": convoy_quests,
        "ballads": [],
        "tributes": [],
        "tallies": [],
        "penances": [],
        "opinions": [],
        "commutations": [],
        "commutations_done": [],
        "extra_tributes": [],
    }

    for q in convoy_quests:
        b = q.extract_tribute_subsection("ballad")
        if b:
            manifest["ballads"].append((q, b))
        t = q.extract_tribute_subsection("tribute")
        if t:
            manifest["tributes"].append((q, t))
        v = q.extract_tribute_subsection("tally")
        if v:
            manifest["tallies"].append((q, v))
        p = q.extract_tribute_subsection("penance")
        if p:
            manifest["penances"].append((q, p))
        o = q.extract_tribute_subsection("opinion")
        if o:
            manifest["opinions"].append((q, o))
        c = q.extract_commutation()
        if c:
            if q.commutation_complete():
                manifest["commutations_done"].append((q, c))
            else:
                manifest["commutations"].append((q, c))
        et = q.extract_extra_tribute()
        if et:
            manifest["extra_tributes"].append((q, et))

    return manifest


def __getattr__(name: str) -> Any:
    if name == "QUESTS_DIR":
        return get_quests_dir()
    if name == "EPICS_DIR":
        return get_epics_dir()
    if name == "ARCHIVE_DIR":
        return get_archive_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
