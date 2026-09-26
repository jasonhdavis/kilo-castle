"""Deterministic migration-collision guardrails for convoy packing.

Closes the same-number / two-leaf packing failure class mechanically at
`court collect` time instead of via prose directives (prose erodes: nine
worktrees carried quest-local merge migrations into the 2026-09-14 convoy
wave despite a standing charter directive, and the v1383 production deploy
aborted on a two-leaf 0130 fork that git cannot see).

The three collision shapes this module participates in:

  Shape A - two leaves, distinct filenames: git merges clean; Django refuses
      the multi-leaf graph at graph-build time. Resolved by the Gatekeeper
      (one trunk-owned merge node generated in the convoy after ALL candidate
      branches are merged) per the amended gatekeeper review template.
  Shape B - same filename (git add/add), or conflicting ops on one model:
      the merge physically cannot proceed. Trunk-side migration wins; the
      loser is rejected and regenerates (delete-and-regenerate procedure in
      serf_remediation_prompt.md).
  Shape C - quest-local NNNN_merge_* contraband: `court collect` refuses to
      pack the carrier branch. THIS module's scan.

Detection is DIFFERENTIAL: a candidate branch is scanned for migration files
it ADDS relative to the base trunk (`git diff --diff-filter=A base...branch`),
so castle's own pre-existing merge nodes never flag innocent branches.

Stdlib + git only. No Django import: detection is filename- and content-
shaped (a merge node is a zero-operation migration depending on two or more
same-app migrations), which also catches custom-named merge nodes that the
`NNNN_merge_*` filename pattern misses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from court import git_ops

# apps/<app>/migrations/<file>.py - the Django app layout this castle uses.
_MIGRATIONS_PATH_RE = re.compile(r"^apps/([^/]+)/migrations/([^/]+\.py)$")
# makemigrations --merge default name: NNNN_merge_<suffix>.py
_MERGE_FILENAME_RE = re.compile(r"^\d{4,}_merge(_[^/]*)?\.py$")
# A real (numbered) migration file; excludes __init__.py and stray scratch.
_NUMBERED_MIGRATION_RE = re.compile(r"^\d{4,}_.*\.py$")
# Merge-shaped content: zero operations, >= 2 dependencies on the same app.
_EMPTY_OPS_RE = re.compile(r"operations\s*=\s*\[\s*\]")
_DEPENDENCIES_BLOCK_RE = re.compile(r"dependencies\s*=\s*\[(.*?)\]", re.DOTALL)
_DEP_TUPLE_RE = re.compile(r"\(\s*['\"]([\w.]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)")


@dataclass
class ContrabandHit:
    """One quest-local merge migration found on a candidate branch."""

    path: str
    app: str
    reason: str  # "filename-merge" (NNNN_merge_*) or "zero-op-merge" (shaped)


def migration_app(path: str) -> Optional[str]:
    """Return the Django app name for a path under apps/<app>/migrations/, else None."""
    m = _MIGRATIONS_PATH_RE.match(path.replace("\\", "/"))
    return m.group(1) if m else None


def is_migration_file(path: str) -> bool:
    """True for numbered migration files (excludes __init__.py and scratch)."""
    m = _MIGRATIONS_PATH_RE.match(path.replace("\\", "/"))
    return bool(m and _NUMBERED_MIGRATION_RE.match(m.group(2)))


def is_merge_migration_filename(path: str) -> bool:
    """True for the standard makemigrations --merge filename (NNNN_merge_*.py)."""
    m = _MIGRATIONS_PATH_RE.match(path.replace("\\", "/"))
    return bool(m and _MERGE_FILENAME_RE.match(m.group(2)))


def is_merge_shaped_content(text: str, app: str) -> bool:
    """True when the migration text is a merge node: zero operations plus two
    or more dependencies on the same app. Catches custom-named merge nodes
    (e.g. `0077_cogship081_marketing_leaf_merge.py`) the filename rule misses."""
    if not _EMPTY_OPS_RE.search(text):
        return False
    block = _DEPENDENCIES_BLOCK_RE.search(text)
    if not block:
        return False
    same_app_deps = [a for a, _ in _DEP_TUPLE_RE.findall(block.group(1)) if a == app]
    return len(same_app_deps) >= 2


def _git_args_ok(res: dict) -> list[str]:
    return res.get("stdout", "").splitlines() if res.get("ok") else []


def _added_migration_paths(root: Path, base_ref: str, branch_ref: str) -> list[str]:
    """Migration files the branch ADDS relative to the merge-base with base_ref."""
    res = git_ops._run(
        ["git", "diff", "--name-only", "--diff-filter=A", f"{base_ref}...{branch_ref}"],
        cwd=root,
    )
    return [p for p in _git_args_ok(res) if is_migration_file(p)]


def _read_blob(root: Path, ref: str, path: str) -> str:
    res = git_ops._run(["git", "show", f"{ref}:{path}"], cwd=root)
    return res.get("stdout", "") if res.get("ok") else ""


def scan_branch_contraband(root: Path, base_ref: str, branch_ref: str) -> list[ContrabandHit]:
    """Return quest-local merge migrations the branch adds over base_ref (Shape C).

    Raises nothing: git failures surface as an empty scan (fail-open) because the
    Gatekeeper graph check is the downstream backstop; callers print a warning
    when they need the distinction.
    """
    hits: list[ContrabandHit] = []
    for path in _added_migration_paths(root, base_ref, branch_ref):
        app = migration_app(path) or ""
        if is_merge_migration_filename(path):
            hits.append(ContrabandHit(path=path, app=app, reason="filename-merge"))
            continue
        if is_merge_shaped_content(_read_blob(root, branch_ref, path), app):
            hits.append(ContrabandHit(path=path, app=app, reason="zero-op-merge"))
    return hits


def migration_activity(
    root: Path, branches: Iterable[tuple[str, str]], base_ref: str = "castle"
) -> dict[str, list[dict]]:
    """Group migration files each in-flight branch ADDS over base_ref, by app.

    `branches` yields (quest_id, branch_ref) pairs. Returns
    {app: [{"quest_id", "branch", "path"}]} - the charter-time advisory's
    collision population (a pigeonhole problem: N quests off one castle tip
    each autogenerate the same next number).
    """
    activity: dict[str, list[dict]] = {}
    for quest_id, branch_ref in branches:
        try:
            added = _added_migration_paths(root, base_ref, branch_ref)
        except Exception:
            continue
        for path in added:
            app = migration_app(path)
            if app:
                activity.setdefault(app, []).append(
                    {"quest_id": quest_id, "branch": branch_ref, "path": path}
                )
    return activity


def remediation_message(hits: list[ContrabandHit], base_ref: str = "castle") -> str:
    """Compact one-line skip reason (collect prints `- <qid>: <reason>`)."""
    files = ", ".join(h.path for h in hits)
    return (
        f"quest-local merge migration(s) are contraband: {files}; excise (git rm), "
        f"git merge {base_ref}, then delete-and-regenerate: schema migrations via "
        f"`makemigrations <app>` (it computes the next free number and deps), data "
        f"migrations via rename + hand-repoint dependencies; NEVER create NNNN_merge_* "
        f"in a Quest worktree (trunk-owned merge nodes are generated in the convoy)"
    )
