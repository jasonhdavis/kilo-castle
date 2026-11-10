"""Gatehouse reversibility: peel a promoted Quest back out of the trunk.

Promotion is executed per branch, so a Quest's commits travel into ``castle`` as
part of the convoy's history. When a Quest must be withdrawn *after* that — a
pillory, a royal ruling, a discovery that its code is wrong — the trunk invariant
in :mod:`court.convoy_guard` reports "code in the trunk with no promotion
decision" and there is no honest way out: un-pillorifying lies about the ruling,
and deleting the quest branch only hides the guard.

``court peel`` is the missing verb. It reverts the Quest's promotion commit on
the trunk (content-level, so the rejected code cannot ship), records the
withdrawal in ``.court/peels/<QUEST>.json``, and supersedes the convoy's suite
proof. The guard treats a recorded peel as withdrawn, and ``court collect``
clears the record when the Quest is packed again — peeling off and re-packing.

Peel sources, in order of preference:

* an explicit ``commit`` (``court peel Q123 --commit <sha>``): the promotion
  merge, or the Quest's own code commit when its convoy was entangled with
  another Quest's promotion;
* the Quest's recorded ``cogship_promoted_commit`` when that commit is a merge
  whose source branch is the Quest itself;
* the newest merge on the trunk whose subject names the Quest branch.

The command never guesses a commit it cannot name in its output: an entangled
convoy (a Quest riding another Quest's promotion merge) fails closed and asks
for ``--commit`` rather than reverting a sibling's code.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import convoy_guard
from . import git_ops

_BRANCH_MERGE_RE = convoy_guard._BRANCH_MERGE_RE

# Conflicts here are bookkeeping, never shipped code: a Quest commit touches
# ``.kilo/TASK.md`` or a board snapshot, later commits rewrite it, and the
# reverse cannot apply cleanly. Keeping the trunk's current content is correct;
# a conflict in production code is not resolvable this way and still fails closed.
_BOOKKEEPING_PREFIXES = (".kilo/", ".court/")


@dataclass
class PeelPlan:
    quest: str
    branch: str
    commit: str
    subject: str
    mainline: Optional[int]
    method: str
    note: str = ""


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
    )


def _rev(repo_root: Path, ref: str) -> str:
    res = _git(repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    return res.stdout.strip() if res.returncode == 0 else ""


def _parents(repo_root: Path, sha: str) -> list[str]:
    res = _git(repo_root, "rev-list", "--parents", "-n", "1", sha)
    if res.returncode != 0:
        return []
    return res.stdout.strip().split()[1:]


def _subject(repo_root: Path, sha: str) -> str:
    res = _git(repo_root, "log", "-1", "--format=%s", sha)
    return res.stdout.strip() if res.returncode == 0 else ""


def _is_ancestor(repo_root: Path, ref: str, trunk: str) -> bool:
    return _git(repo_root, "merge-base", "--is-ancestor", ref, trunk).returncode == 0


def _quest_branch(quest) -> str:
    return quest.branch or getattr(quest, "tree_branch", "") or ""


def find_quest_merge(repo_root: Path, trunk: str, branch: str) -> str:
    """Newest trunk merge whose subject names ``branch`` as the merged side."""
    if not branch:
        return ""
    res = _git(repo_root, "log", "--merges", "--max-count=4000", "--format=%H%x09%s", trunk)
    if res.returncode != 0:
        return ""
    for line in res.stdout.splitlines():
        if "\t" not in line:
            continue
        sha, subject = line.split("\t", 1)
        match = _BRANCH_MERGE_RE.match(subject.strip())
        if not match:
            continue
        if match.group(1).strip() == branch.strip():
            return sha
    return ""


def _promotion_present(repo_root: Path, quest, branch: str, trunk: str) -> bool:
    """True when any promotion handle for this Quest is in the trunk.

    A Quest branch tip is often *not* an ancestor afterwards: the Court keeps
    committing bookkeeping (status advances, side-state syncs) on the branch
    after promotion. The recorded ``cogship_promoted_commit`` still proves the
    code entered the trunk, so it is the primary handle.
    """
    if branch and _is_ancestor(repo_root, branch, trunk):
        return True
    recorded = str(getattr(quest, "cogship_promoted_commit", "") or "").strip()
    if not recorded:
        return False
    sha = _rev(repo_root, recorded)
    return bool(sha and _is_ancestor(repo_root, sha, trunk))


def plan_peel(
    repo_root: Path,
    quest,
    trunk: str = "castle",
    commit: str = "",
) -> PeelPlan:
    """Resolve the commit whose reverse removes this Quest from ``trunk``.

    Raises ``ValueError`` when the Quest is not in the trunk or no commit can be
    attributed to it without guessing (an entangled convoy).
    """
    qid = quest.id.split("-")[0].upper()
    branch = _quest_branch(quest)

    if commit:
        sha = _rev(repo_root, commit)
        if not sha:
            raise ValueError(f"'{commit}' does not resolve to a commit")
        if not _is_ancestor(repo_root, sha, trunk):
            raise ValueError(f"{sha[:12]} is not an ancestor of {trunk} — its reverse would not peel this Quest")
        mainline = 1 if len(_parents(repo_root, sha)) > 1 else None
        return PeelPlan(qid, branch, sha, _subject(repo_root, sha), mainline, "explicit --commit")

    if not _promotion_present(repo_root, quest, branch, trunk):
        raise ValueError(f"{qid} ({branch or 'no branch'}) is not merged into {trunk} — nothing to peel")

    recorded = str(getattr(quest, "cogship_promoted_commit", "") or "").strip()
    if recorded:
        sha = _rev(repo_root, recorded)
        if sha and _is_ancestor(repo_root, sha, trunk):
            subject = _subject(repo_root, sha)
            match = _BRANCH_MERGE_RE.match(subject)
            parents = _parents(repo_root, sha)
            # Only revert the recorded tip when it is the Quest's own merge into
            # a convoy/trunk. A convoy tip that merged several Quests would take
            # siblings down with it.
            if len(parents) > 1 and match and match.group(1).strip() == branch.strip():
                return PeelPlan(qid, branch, sha, subject, 1, "recorded cogship_promoted_commit")

    merge_sha = find_quest_merge(repo_root, trunk, branch)
    if merge_sha:
        return PeelPlan(qid, branch, merge_sha, _subject(repo_root, merge_sha), 1, "trunk merge by branch name")

    raise ValueError(
        f"no commit on {trunk} can be attributed to {qid} without guessing — its convoy was "
        f"entangled with another Quest's promotion. Re-run with --commit <sha> naming the "
        f"Quest's own code commit (find it with: git log --oneline {trunk} --grep {qid.split('-')[0]})."
    )


def _trunk_worktree(repo_root: Path, trunk: str) -> Optional[Path]:
    for wt in git_ops.list_git_worktrees(repo_root, use_cache=False):
        if wt.get("branch") == trunk and not wt.get("bare"):
            path = Path(wt["worktree"])
            if path.is_dir():
                return path
    return None


def _supersede_suite_proof(repo_root: Path, cogship_id: str) -> str:
    """Retire the convoy's green proof: it was stamped against a tree that still
    carried this Quest, so the re-packed convoy must be re-verified."""
    cid = convoy_guard.normalize_cogship_id(cogship_id or "")
    if not cid:
        return ""
    path = Path(repo_root) / ".court" / "suites" / f"{cid}.json"
    if not path.is_file():
        return ""
    stamp = convoy_guard._now_iso().replace(":", "").replace("-", "")
    retired = path.with_name(f"{path.name}.superseded-{stamp}")
    try:
        shutil.move(str(path), str(retired))
    except OSError:
        return ""
    return str(retired.relative_to(repo_root))


def peel_quest(
    repo_root: Path,
    quest,
    trunk: str = "castle",
    commit: str = "",
    reason: str = "",
    dry_run: bool = False,
) -> dict:
    """Revert a promoted Quest out of the trunk and record the withdrawal."""
    plan = plan_peel(repo_root, quest, trunk=trunk, commit=commit)
    result = {
        "ok": True,
        "quest": plan.quest,
        "branch": plan.branch,
        "commit": plan.commit,
        "subject": plan.subject,
        "method": plan.method,
        "mainline": plan.mainline,
        "dry_run": dry_run,
    }
    if dry_run:
        return result

    worktree = _trunk_worktree(repo_root, trunk)
    if worktree is None:
        raise ValueError(f"no worktree has '{trunk}' checked out — a revert needs a real working tree")
    dirty = _git(worktree, "status", "--porcelain").stdout.strip()
    if dirty:
        raise ValueError(f"{worktree} has uncommitted changes — commit or stash them before peeling")

    revert = ["git", "-C", str(worktree), "revert", "--no-commit"]
    if plan.mainline is not None:
        revert += ["-m", str(plan.mainline)]
    revert.append(plan.commit)
    res = subprocess.run(revert, capture_output=True, text=True)
    bookkeeping_conflicts: list[str] = []
    if res.returncode != 0:
        conflicts = [
            p.strip()
            for p in _git(worktree, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
            if p.strip()
        ]
        if not conflicts or not all(p.startswith(_BOOKKEEPING_PREFIXES) for p in conflicts):
            _git(worktree, "revert", "--abort")
            raise ValueError(
                "revert conflicted"
                + (f" in: {', '.join(conflicts)}" if conflicts else "")
                + " — resolve the Quest's code out by hand, commit, then record the peel with "
                "--commit <that commit>"
            )
        # Bookkeeping-only conflicts: keep the trunk's current content.
        for path in conflicts:
            _git(worktree, "checkout", "--ours", "--", path)
            _git(worktree, "add", "--", path)
            bookkeeping_conflicts.append(path)

    peel_path = convoy_guard.peel_record_path(repo_root, plan.quest)
    peel_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "quest": plan.quest,
        "branch": plan.branch,
        "trunk": trunk,
        "source_commit": plan.commit,
        "source_subject": plan.subject,
        "method": plan.method,
        "mainline": plan.mainline,
        "reason": reason,
        "bookkeeping_conflicts_kept": bookkeeping_conflicts,
        "peeled_at": convoy_guard._now_iso(),
    }
    peel_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    retired = _supersede_suite_proof(repo_root, getattr(quest, "cogship_id", "") or "")
    if retired:
        result["superseded_suite_proof"] = retired

    _git(worktree, "add", "-A")
    summary = f"court: peel {plan.quest} out of {trunk}"
    if reason:
        summary += f" — {reason}"
    summary += f"\n\nPeeled-Quest: {plan.quest}\nSource-Commit: {plan.commit}\n"
    commit_res = _git(worktree, "commit", "-m", summary)
    if commit_res.returncode != 0:
        _git(worktree, "revert", "--abort")
        raise ValueError(f"peel commit failed: {commit_res.stderr.strip() or commit_res.stdout.strip()}")

    revert_sha = _rev(worktree, "HEAD")
    record["revert_sha"] = revert_sha
    peel_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _git(worktree, "add", str(peel_path))
    _git(
        worktree,
        "commit",
        "-m",
        f"court: record peel {plan.quest}\n\nPeeled-Quest: {plan.quest}\nRevert-Commit: {revert_sha}\n",
    )

    result.update(
        {
            "revert_sha": revert_sha,
            "peel_record": str(peel_path),
            "worktree": str(worktree),
            "bookkeeping_conflicts_kept": bookkeeping_conflicts,
        }
    )
    return result


def list_peels(repo_root: Path) -> list[dict]:
    directory = Path(repo_root) / ".court" / "peels"
    if not directory.is_dir():
        return []
    rows: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            payload.setdefault("quest", path.stem)
            payload["active"] = bool(
                payload.get("revert_sha")
                and _is_ancestor(repo_root, str(payload["revert_sha"]), str(payload.get("trunk") or "castle"))
            )
            rows.append(payload)
    return rows
