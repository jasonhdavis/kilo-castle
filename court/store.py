"""
Filesystem storage and persistence for Quests and Epics under .court/.

Directory layout:
    .court/quests/    Active + completed Quests (Q001-App-Concern.md)
    .court/epics/     Epic Quests (Q0NN-App-Concern.md, kind=epic)
    .court/archive/   Archived Quests/Epics (still readable)
    .court/templates/ Prompt dispatch and review templates
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Optional

from court.models import Quest

_ID_NUM_RE = re.compile(r"^Q(\d+)-")


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


def save(quest: Quest, court_root: Optional[Path] = None) -> Path:
    p = path_for(quest, court_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(quest.to_markdown(), encoding="utf-8")
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
                # Log warning to stderr without crashing listing
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
