"""
Filesystem storage and persistence for Quests and Epics under .court/.

Directory layout:
    .court/quests/    Active + completed Quests (Q001-App-Concern.md)
    .court/epics/     Epic Quests (Q0NN-App-Concern.md, kind=epic)
    .court/archive/   Archived Quests/Epics (still readable)
    .court/templates/ Prompt dispatch and review templates
    .court/EDICTS.md  Royal decrees and strategic priorities
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

from court import git_ops
from court.models import Quest

_ID_NUM_RE = re.compile(r"^Q(\d+)-")
_COGSHIP_NUM_RE = re.compile(r"cogship[-_]?(\d+)", re.IGNORECASE)
_COGSHIP_FM_RE = re.compile(r"^cogship_id:\s*(\S.*)$", re.MULTILINE)

# Branches whose worktree is a shared/canonical checkout (the ledger of
# record for batch/automation commands like `court levy`), as opposed to a
# single Quest's own disposable worktree. Auto-commit from `store.save()` is
# only ever safe from one of these, or from that exact Quest's own branch —
# see `_commit_allowed_here()`.
PROTECTED_TRUNK_BRANCHES = ("castle", "main")
PROTECTED_BRANCH_PREFIXES = ("the-gatehouse/",)


def _is_protected_branch(branch: str) -> bool:
    if branch in PROTECTED_TRUNK_BRANCHES:
        return True
    return any(branch.startswith(p) for p in PROTECTED_BRANCH_PREFIXES)


def _current_repo_branch(repo_root: Path) -> str:
    """Branch checked out at `repo_root` (the current worktree, or a
    protected trunk). Recomputed on every call rather than cached, since a
    single process may legitimately operate against more than one
    `court_root`/repo (tests, multi-repo tooling)."""
    res = git_ops._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    return res.get("stdout", "").strip() if res.get("ok") else ""


def _commit_allowed_here(quest: Quest, court_root: Optional[Path] = None) -> bool:
    """Guard against cross-branch pollution: `save()`'s auto-commit path
    commits from wherever this process is physically checked out
    (`court_root.parent`) — the *current* worktree, not necessarily the
    worktree of the Quest actually being saved. A multi-quest bulk command
    (e.g. `court levy`, which iterates every WORKING Quest) run from inside
    Quest A's own worktree would otherwise read Quest B's real tribute
    correctly (via git-worktree branch matching) but then commit it onto
    Quest A's *own* branch — because that's where this process happens to be
    checked out. That silently poisons Quest A's branch with phantom "court:
    save Q-B" commits that later produce real merge conflicts against the
    protected trunk's legitimate forward progress on Quest B's file.

    Auto-committing a Quest's file is only safe when the current checkout is
    a protected trunk (`castle`, `main`, or a `the-gatehouse/<station>`
    integration branch — the canonical ledger of record for batch/automation
    commands) or that exact Quest's own registered branch (a Serf editing
    its own Quest from its own worktree).
    """
    root = court_root or get_court_root()
    repo_root = root.parent
    current = _current_repo_branch(repo_root)
    if not current:
        # Can't determine the current branch (detached HEAD, git error, bare
        # checkout) — fail closed rather than risk a silent cross-branch write.
        return False
    if _is_protected_branch(current):
        return True
    return bool(quest.branch) and current == quest.branch


def get_court_root(start_path: Optional[Path] = None) -> Path:
    """Locate the .court root directory by searching upwards from start_path or cwd,
    or reading COURT_DIR from environment."""
    env_dir = os.environ.get("COURT_DIR")
    if env_dir:
        return Path(env_dir).resolve()

    current = (start_path or Path.cwd()).resolve()
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


def next_number(court_root: Optional[Path] = None) -> int:
    """Single global counter shared by Quests and Epics (deterministic scan
    of every markdown filename currently on disk, including archive — never
    reused even after archiving)."""
    highest = 0
    for d in all_state_dirs(court_root):
        for p in d.glob("Q*.md"):
            m = _ID_NUM_RE.match(p.stem)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


def slugify_title(app: str, concern: str) -> str:
    """Convert app and concern slugs into Title-Cased hyphenated string."""
    parts = re.split(r"[\s_/-]+", f"{app} {concern}".strip())
    parts = [p for p in parts if p]
    return "-".join(p[:1].upper() + p[1:] for p in parts)


def make_id(app: str, concern: str, number: Optional[int] = None, court_root: Optional[Path] = None) -> str:
    n = number if number is not None else next_number(court_root)
    return f"Q{n:03d}-{slugify_title(app, concern)}"


def normalize_cogship_id(value: Optional[str]) -> Optional[str]:
    """Normalize a user-supplied Cog Ship identifier (a bare number, or any
    string containing "cogship[-_]NNN") to the canonical "cogship-NNN" form."""
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
            except OSError:
                continue
            for m in _COGSHIP_FM_RE.finditer(text):
                n = normalize_cogship_id(m.group(1))
                if n:
                    highest = max(highest, int(n.split("-")[1]))
    return highest


def next_cogship_id(court_root: Optional[Path] = None) -> str:
    """Allocate the next monotonic Cog Ship id (cogship-NNN) by scanning
    existing frontmatter across disk. Never reused, mirroring `next_number()`."""
    return f"cogship-{_scan_existing_cogship_numbers(court_root) + 1:03d}"


def stamp_cogship(
    quests: Iterable[Quest],
    cogship_id: Optional[str] = None,
    court_root: Optional[Path] = None,
    auto_commit: bool = False,
    commit_msg: Optional[str] = None,
) -> str:
    """Stamp a cogship_id onto a batch of Quest objects and persist them.

    Refuses to stamp archived quests. If cogship_id is None, allocates a new
    monotonic id (cogship-NNN) by scanning existing frontmatter across disk.
    """
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
        q.append_history(
            q.status,
            q.status,
            f"Stamped onto {cogship_id}"
            + (f" (reassigned from {prior})" if prior and prior != cogship_id else ""),
        )
        msg = commit_msg or f"court: stamp {q.id} onto {cogship_id}"
        save(q, court_root=root, auto_commit=auto_commit, commit_msg=msg)
    return cogship_id


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
    # Allow lookup by bare number (e.g. "12" or "Q012")
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


def save(
    quest: Quest,
    court_root: Optional[Path] = None,
    auto_commit: bool = False,
    commit_msg: Optional[str] = None,
) -> Path:
    """Write a Quest/Epic's markdown to disk.

    By default (`auto_commit=False`) this is a plain, unconditional write —
    byte-for-byte identical to the historical behavior every existing caller
    depends on. Pass `auto_commit=True` to additionally stage+commit just
    this file via `git_ops.git_commit_paths()`, gated by `_commit_allowed_here()`
    to guard against the cross-branch pollution failure mode documented there.
    """
    root = court_root or get_court_root()
    p = path_for(quest, root)
    if auto_commit and not _commit_allowed_here(quest, court_root=root):
        # Check *before* writing to disk: writing here even without
        # committing would still leave a real uncommitted diff on whatever
        # branch this process is checked out on, which is just as capable of
        # poisoning that branch's next rebase — so treat an out-of-scope
        # save as a full no-op, not merely an uncommitted one.
        current = _current_repo_branch(root.parent) or "(unknown/detached)"
        print(
            f"WARNING: refusing to save {quest.id} from branch '{current}' — it is "
            f"neither a protected trunk (castle/main/the-gatehouse/*) nor {quest.id}'s "
            f"own branch ({quest.branch or 'unset'}). Re-run this from a protected "
            f"trunk or {quest.id}'s own worktree to auto-commit it.",
            file=sys.stderr,
        )
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(quest.to_markdown(), encoding="utf-8")
    if auto_commit:
        msg = commit_msg or f"court: save {quest.id}"
        res = git_ops.git_commit_paths([p], msg, cwd=root.parent)
        if not res.get("ok") and not res.get("no_changes"):
            warning = res.get("warning") or res.get("stderr") or "unknown git error"
            print(f"WARNING: autocommit failed for {p.name}: {warning}", file=sys.stderr)
    return p


def load(quest_id: str, court_root: Optional[Path] = None) -> Quest:
    p = find_path(quest_id, court_root)
    if p is None:
        raise FileNotFoundError(f"No quest/epic file found for id {quest_id!r}")
    return Quest.from_markdown(p.read_text(encoding="utf-8"))


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
                out.append(Quest.from_markdown(p.read_text(encoding="utf-8")))
            except Exception as e:
                print(f"WARNING: failed to parse {p}: {e}")
    return sorted(out, key=lambda q: q.id)


def archive(quest_id: str, court_root: Optional[Path] = None) -> Path:
    root = court_root or get_court_root()
    src = find_path(quest_id, root)
    archive_d = get_archive_dir(root)
    if src is None or src.parent == archive_d:
        raise FileNotFoundError(f"No active quest/epic file found for id {quest_id!r}")
    archive_d.mkdir(parents=True, exist_ok=True)
    dst = archive_d / src.name
    src.rename(dst)
    return dst


def rollup_section(
    section_name: str,
    app: Optional[str] = None,
    epic: Optional[str] = None,
    status: Optional[str] = None,
    include_archive: bool = False,
    court_root: Optional[Path] = None,
) -> list[tuple[Quest, str]]:
    """Roll up a specific tribute subsection across Quests matching filters."""
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

    results = []
    for q in quests:
        content = q.extract_tribute_subsection(section_name)
        if content:
            results.append((q, content))
    return results


def get_hierarchy(include_archive: bool = False, court_root: Optional[Path] = None) -> dict:
    """Returns a structured hierarchy:
    - epics: list of (epic_quest, list_of_child_quests)
    - standalone: list of quests without parent_epic (not epic, not scout/investigation)
    - scouts: list of scout/investigation quests
    """
    quests = list_all(include_archive=include_archive, court_root=court_root)
    epics = [q for q in quests if q.kind == "epic"]
    scouts = [q for q in quests if q.kind == "scout" or q.section == "Investigation"]

    epic_map: dict[str, list[Quest]] = {e.id: [] for e in epics}
    epic_short_map: dict[str, str] = {e.id.split("-")[0].lower(): e.id for e in epics}

    standalone: list[Quest] = []
    for q in quests:
        if q.kind == "epic":
            continue
        if q.kind == "scout" or q.section == "Investigation":
            continue
        if q.parent_epic:
            parent_key = q.parent_epic.strip()
            parent_short = parent_key.split("-")[0].lower()
            if parent_key in epic_map:
                epic_map[parent_key].append(q)
            elif parent_short in epic_short_map:
                epic_map[epic_short_map[parent_short]].append(q)
            else:
                standalone.append(q)
        else:
            standalone.append(q)

    epic_pairs = [(e, epic_map.get(e.id, [])) for e in epics]
    return {
        "epics": epic_pairs,
        "standalone": standalone,
        "scouts": scouts,
    }


def load_edicts(court_root: Optional[Path] = None) -> str:
    """Load the contents of .court/EDICTS.md."""
    p = get_edicts_path(court_root)
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return ""


def save_edicts(content: str, court_root: Optional[Path] = None) -> Path:
    """Save content to .court/EDICTS.md."""
    p = get_edicts_path(court_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content.strip() + "\n", encoding="utf-8")
    return p


def rollup_ship_manifest(
    app: Optional[str] = None,
    epic: Optional[str] = None,
    status: Optional[str] = None,
    include_archive: bool = False,
    court_root: Optional[Path] = None,
) -> dict:
    """Extract and aggregate all four rollups (ballad, tribute, penance, opinion)
    for Quests in the Cog Ship deployment convoy — the tribute entering the castle
    ahead of promoting `castle` into `main`.
    Defaults to Quests with status READY_FOR_TEARDOWN or DONE if status is not specified.
    """
    if status is None:
        target_statuses = {"READY_FOR_TEARDOWN", "DONE"}
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

    convoy_quests = [q for q in all_quests if q.status in target_statuses]

    manifest = {
        "quests": convoy_quests,
        "ballads": [],
        "tributes": [],
        "tallies": [],
        "penances": [],
        "opinions": [],
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
        p_ = q.extract_tribute_subsection("penance")
        if p_:
            manifest["penances"].append((q, p_))
        o = q.extract_tribute_subsection("opinion")
        if o:
            manifest["opinions"].append((q, o))

    return manifest
