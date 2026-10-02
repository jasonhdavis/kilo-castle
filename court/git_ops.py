"""
Deterministic git/test verification helpers. No LLM calls here — these are
the facts a Tribute review is checked against. All functions are subprocess
wrappers that return plain dicts/booleans.
"""
from __future__ import annotations

import fcntl
import json
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import migration_graph
from .config import get_suite_command

# Short-TTL process-level cache for the two most repeated git lookups
# (`rev-parse --show-toplevel` and `worktree list`). A single levy/collect run
# re-resolves worktrees several times per Quest; each miss costs 1-3 git
# subprocess spawns. TTL bounds staleness; mutation paths that change the
# worktree set call clear_git_cache().
_GIT_CACHE: dict[str, tuple[float, Any]] = {}
_GIT_CACHE_TTL_SECONDS = 1.5
_GIT_CACHE_LOCK = threading.Lock()


def _cache_get(key: str) -> Any:
    now = time.monotonic()
    with _GIT_CACHE_LOCK:
        hit = _GIT_CACHE.get(key)
        if hit and (now - hit[0]) <= _GIT_CACHE_TTL_SECONDS:
            return hit[1]
    return None


def _cache_put(key: str, value: Any) -> None:
    with _GIT_CACHE_LOCK:
        _GIT_CACHE[key] = (time.monotonic(), value)


def clear_git_cache() -> None:
    with _GIT_CACHE_LOCK:
        _GIT_CACHE.clear()

_CONFLICT_BLOCK_RE = re.compile(
    r"<<<<<<< [^\n]*\n(?P<ours>.*?)\n=======\n(?P<theirs>.*?)\n>>>>>>> [^\n]*",
    re.DOTALL,
)
_HISTORY_ROW_RE = re.compile(r"^\|\s*([0-9T:\-Z]+)\s*\|.*\|\s*$")
_LEDGER_BULLET_ROW_RE = re.compile(r"^[-*]\s+\*\*\[?\(?([0-9T:\-Z]+)\)?\]?\*\*\s*[—:-]")


def _ledger_row_timestamp(line: str) -> Optional[str]:
    m = _HISTORY_ROW_RE.match(line)
    if m:
        return m.group(1)
    m = _LEDGER_BULLET_ROW_RE.match(line)
    if m:
        return m.group(1)
    return None


def _is_ledger_row(line: str) -> bool:
    return _ledger_row_timestamp(line) is not None


def _reconcile_history_only_conflict(path: Path) -> bool:
    """For a Quest/Epic ledger file conflicted *only* in its History-table
    rows / bulleted Castle Ledger rows and/or its `updated_at:` frontmatter
    line, resolve by taking the union of both sides' rows (deduped, sorted by
    timestamp) and the later `updated_at`."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if "<<<<<<<" not in text:
        return False

    def _resolve_block(m: "re.Match[str]") -> Optional[str]:
        ours_lines = m.group("ours").splitlines()
        theirs_lines = m.group("theirs").splitlines()

        if (
            len(ours_lines) == 1 and len(theirs_lines) == 1
            and ours_lines[0].startswith("updated_at:")
            and theirs_lines[0].startswith("updated_at:")
        ):
            ours_ts = ours_lines[0].split(":", 1)[1].strip()
            theirs_ts = theirs_lines[0].split(":", 1)[1].strip()
            return f"updated_at: {max(ours_ts, theirs_ts)}"

        all_rows = [l for l in ours_lines + theirs_lines if l.strip()]
        if all_rows and all(_is_ledger_row(l) for l in all_rows):
            merged = list(dict.fromkeys(ours_lines + theirs_lines))
            merged.sort(key=lambda l: _ledger_row_timestamp(l) or "")
            return "\n".join(merged)

        return None

    out_parts = []
    pos = 0
    for m in _CONFLICT_BLOCK_RE.finditer(text):
        resolved = _resolve_block(m)
        if resolved is None:
            return False
        out_parts.append(text[pos:m.start()])
        out_parts.append(resolved)
        pos = m.end()
    out_parts.append(text[pos:])
    new_text = "".join(out_parts)
    if "<<<<<<<" in new_text or ">>>>>>>" in new_text:
        return False

    path.write_text(new_text, encoding="utf-8")
    return True


def _run(cmd: list[str], cwd: Path, timeout: int = 60) -> dict:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "cmd": " ".join(cmd),
            "exit_code": proc.returncode,
            "stdout": proc.stdout.rstrip("\n"),
            "stderr": proc.stderr.rstrip("\n"),
            "ok": proc.returncode == 0,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "cmd": " ".join(cmd),
            "exit_code": -1,
            "stdout": (e.stdout or "").rstrip("\n") if isinstance(e.stdout, str) else "",
            "stderr": f"Command timed out after {timeout}s",
            "ok": False,
            "timed_out": True,
        }
    except Exception as e:
        return {
            "cmd": " ".join(cmd),
            "exit_code": -1,
            "stdout": "",
            "stderr": str(e),
            "ok": False,
        }


def get_repo_root(cwd: Optional[Path | str] = None, use_cache: bool = True) -> Path:
    """Find the top-level directory of the current git repository."""
    base = Path(cwd) if cwd else Path.cwd()
    cache_key = f"root:{base.resolve()}"
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
    res = _run(["git", "rev-parse", "--show-toplevel"], base)
    if res.get("ok") and res.get("stdout"):
        root = Path(res["stdout"])
        if use_cache:
            _cache_put(cache_key, root)
        return root
    return Path(__file__).resolve().parent.parent


def verify_commit_is_ancestor(sha: str, ref: str, cwd: Optional[Path | str] = None) -> dict:
    """Independently verify that commit `sha` really is an ancestor of `ref`."""
    root = get_repo_root(cwd)
    sha_res = _run(["git", "rev-parse", "--verify", f"{sha}^{{commit}}"], root)
    if not sha_res.get("ok"):
        return {
            "ok": False, "is_ancestor": False,
            "error": f"'{sha}' does not resolve to a real commit in this repository.",
        }
    resolved_sha = sha_res["stdout"].strip()

    ref_res = _run(["git", "rev-parse", "--verify", ref], root)
    if not ref_res.get("ok"):
        ref_res = _run(["git", "rev-parse", "--verify", f"refs/heads/{ref}"], root)
        if not ref_res.get("ok"):
            return {"ok": False, "is_ancestor": False, "error": f"ref '{ref}' does not resolve."}

    anc_res = _run(["git", "merge-base", "--is-ancestor", resolved_sha, ref], root)
    is_ancestor = anc_res.get("exit_code") == 0
    return {
        "ok": True,
        "is_ancestor": is_ancestor,
        "resolved_sha": resolved_sha,
        "ref": ref,
        "error": None if is_ancestor else f"{resolved_sha[:12]} is NOT an ancestor of {ref} — it is not actually merged.",
    }


def list_git_worktrees(cwd: Optional[Path | str] = None, use_cache: bool = True) -> list[dict]:
    """Parse `git worktree list --porcelain` into structured records."""
    root = get_repo_root(cwd, use_cache=use_cache)
    cache_key = f"wtlist:{root}"
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
    res = _run(["git", "worktree", "list", "--porcelain"], root)
    if not res.get("ok"):
        return []

    worktrees: list[dict] = []
    current: dict = {}

    for line in res.get("stdout", "").splitlines():
        line = line.strip()
        if not line:
            if current and "worktree" in current:
                worktrees.append(current)
                current = {}
            continue

        if line.startswith("worktree "):
            if current and "worktree" in current:
                worktrees.append(current)
                current = {}
            current["worktree"] = line[9:].strip()
            current["bare"] = False
            current["detached"] = False
            current["locked"] = False
            current["prunable"] = False
            current["branch"] = ""
            current["raw_branch"] = ""
            current["head"] = ""
        elif line.startswith("HEAD "):
            current["head"] = line[5:].strip()
        elif line.startswith("branch "):
            raw_branch = line[7:].strip()
            current["raw_branch"] = raw_branch
            current["branch"] = raw_branch[11:] if raw_branch.startswith("refs/heads/") else raw_branch
        elif line == "bare":
            current["bare"] = True
        elif line == "detached":
            current["detached"] = True
        elif line.startswith("locked"):
            current["locked"] = True
        elif line.startswith("prunable"):
            current["prunable"] = True

    if current and "worktree" in current:
        worktrees.append(current)

    if use_cache:
        _cache_put(cache_key, worktrees)

    return worktrees


def find_worktree_for_quest(quest: Any, cwd: Optional[Path | str] = None) -> Optional[Path]:
    """Resolve the filesystem path of a Quest's git worktree."""
    worktree_attr = getattr(quest, "worktree", "")
    if worktree_attr:
        p = Path(worktree_attr)
        if p.is_dir():
            return p

    worktrees = list_git_worktrees(cwd)
    quest_id = getattr(quest, "id", str(quest))
    branch = getattr(quest, "branch", "")
    kind = getattr(quest, "kind", "quest")
    short_id = quest_id.split("-")[0].lower()

    if branch:
        for wt in worktrees:
            if wt.get("branch") == branch or wt.get("raw_branch") == f"refs/heads/{branch}":
                p = Path(wt["worktree"])
                if p.is_dir():
                    return p

    for wt in worktrees:
        wt_path = wt.get("worktree", "")
        wt_branch = wt.get("branch", "")

        if kind == "epic":
            if wt_branch.startswith("epic/") and short_id in wt_branch.lower():
                p = Path(wt_path)
                if p.is_dir():
                    return p
            if "epic-" in wt_path.lower() and short_id in wt_path.lower():
                p = Path(wt_path)
                if p.is_dir():
                    return p
            continue

        leaf_branch = wt_branch.split("/")[-1].lower() if "/" in wt_branch else wt_branch.lower()
        leaf_wt = wt_path.split("/")[-1].lower() if "/" in wt_path else wt_path.lower()

        if f"-{short_id}-" in leaf_branch or leaf_branch.startswith(f"{short_id}-") or leaf_branch.endswith(f"-{short_id}") or leaf_branch == short_id:
            p = Path(wt_path)
            if p.is_dir():
                return p

        if f"-{short_id}-" in leaf_wt or leaf_wt.startswith(f"{short_id}-") or leaf_wt.endswith(f"-{short_id}") or leaf_wt == short_id:
            p = Path(wt_path)
            if p.is_dir():
                return p

    return None


def get_worktree_git_status(
    worktree_path: str | Path,
    base: str = "castle",
    include_diffstat: bool = False,
) -> dict:
    """Comprehensive worktree status inspection.

    `include_diffstat` is opt-in: `git diff --stat <base>...HEAD` is O(total
    diff size) and dominates audit latency on drifted branches, while every
    in-repo caller (ward.audit_quest) only consumes dirty/branch/ahead/behind.
    """
    p = Path(worktree_path)
    if not p.exists() or not p.is_dir():
        return {
            "ok": False,
            "exists": False,
            "path": str(worktree_path),
            "branch": "",
            "head_sha": "",
            "dirty": False,
            "untracked": [],
            "modified": [],
            "staged": [],
            "deleted": [],
            "ahead": None,
            "behind": None,
            "diffstat": "",
            "error": f"path does not exist or is not a directory: {worktree_path}",
        }

    branch_res = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], p)
    head_res = _run(["git", "rev-parse", "HEAD"], p)
    status_res = _run(["git", "status", "--porcelain=v1", "-uall"], p)

    untracked: list[str] = []
    modified: list[str] = []
    staged: list[str] = []
    deleted: list[str] = []

    if status_res.get("ok"):
        for raw_line in status_res.get("stdout", "").splitlines():
            if not raw_line or len(raw_line) < 3:
                continue
            x = raw_line[0]
            y = raw_line[1]
            file_path = raw_line[3:].strip()
            if " -> " in file_path:
                file_path = file_path.split(" -> ")[1].strip()

            if x == "?" and y == "?":
                untracked.append(file_path)
            else:
                if x in ("M", "A", "D", "R", "C"):
                    staged.append(file_path)
                if y in ("M", "T"):
                    modified.append(file_path)
                if x == "D" or y == "D":
                    deleted.append(file_path)

    dirty = bool(untracked or modified or staged or deleted)

    ahead_behind_res = _run(["git", "rev-list", "--left-right", "--count", f"{base}...HEAD"], p)
    ahead = behind = None
    if ahead_behind_res.get("ok") and ahead_behind_res.get("stdout"):
        parts = ahead_behind_res["stdout"].split()
        if len(parts) == 2:
            try:
                behind, ahead = int(parts[0]), int(parts[1])
            except ValueError:
                pass

    diffstat_str = ""
    if include_diffstat:
        diffstat_res = _run(["git", "diff", "--stat", f"{base}...HEAD"], p)
        diffstat_str = diffstat_res.get("stdout", "") if diffstat_res.get("ok") else ""

    return {
        "ok": True,
        "exists": True,
        "path": str(p),
        "branch": branch_res.get("stdout", "").strip(),
        "head_sha": head_res.get("stdout", "").strip(),
        "dirty": dirty,
        "untracked": untracked,
        "modified": modified,
        "staged": staged,
        "deleted": deleted,
        "ahead": ahead,
        "behind": behind,
        "diffstat": diffstat_str,
        "error": None,
    }


def get_worktree_diff(
    worktree_path: str | Path,
    base: str = "castle",
    stat_only: bool = False,
    file_stats: bool = True,
) -> dict:
    """Generate structured diff statistics and file inventories against base."""
    p = Path(worktree_path)
    if not p.exists() or not p.is_dir():
        return {
            "ok": False,
            "base": base,
            "patch": "",
            "stat": "",
            "files": [],
            "total_additions": 0,
            "total_deletions": 0,
            "total_files": 0,
            "error": f"path does not exist or is not a directory: {worktree_path}",
        }

    stat_res = _run(["git", "diff", "--stat", f"{base}...HEAD"], p)
    stat_text = stat_res.get("stdout", "") if stat_res.get("ok") else ""

    patch_text = ""
    if not stat_only:
        patch_res = _run(["git", "diff", f"{base}...HEAD"], p)
        patch_text = patch_res.get("stdout", "") if patch_res.get("ok") else ""

    files_list: list[dict] = []
    total_add = 0
    total_del = 0

    if file_stats:
        numstat_res = _run(["git", "diff", "--numstat", f"{base}...HEAD"], p)
        if numstat_res.get("ok") and numstat_res.get("stdout"):
            for line in numstat_res["stdout"].splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split("\t", 2)
                if len(parts) == 3:
                    add_str, del_str, file_name = parts
                    binary = False
                    add_cnt = 0
                    del_cnt = 0
                    if add_str == "-" and del_str == "-":
                        binary = True
                    else:
                        try:
                            add_cnt = int(add_str)
                            del_cnt = int(del_str)
                        except ValueError:
                            pass
                    total_add += add_cnt
                    total_del += del_cnt
                    files_list.append(
                        {
                            "path": file_name,
                            "additions": add_cnt,
                            "deletions": del_cnt,
                            "binary": binary,
                        }
                    )

    return {
        "ok": True,
        "base": base,
        "patch": patch_text,
        "stat": stat_text,
        "files": files_list,
        "total_additions": total_add,
        "total_deletions": total_del,
        "total_files": len(files_list),
        "error": None,
    }


def find_worktree_for_branch(branch_or_id: str, cwd: Optional[Path | str] = None) -> Optional[str]:
    clean = branch_or_id.strip()
    if clean.startswith("refs/heads/"):
        clean = clean[len("refs/heads/"):]
    wts = list_git_worktrees(cwd)
    for wt in wts:
        if wt.get("branch") == clean or wt.get("raw_branch") == f"refs/heads/{clean}":
            return wt.get("worktree")
    for wt in wts:
        wt_path = wt.get("worktree", "")
        if wt_path == clean or wt_path.endswith(f"/{clean}") or Path(wt_path).name == clean:
            return wt_path
    clean_slug = clean.split("/")[-1]
    for wt in wts:
        wt_branch = wt.get("branch", "")
        if wt_branch and wt_branch.split("/")[-1] == clean_slug:
            return wt.get("worktree")
    return None


def worktree_status(worktree_path: str) -> dict:
    p = Path(worktree_path)
    if not p.exists():
        return {"exists": False, "error": f"path does not exist: {worktree_path}"}
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], p)
    status = _run(["git", "status", "--porcelain"], p)
    ahead_behind = _run(["git", "rev-list", "--left-right", "--count", "origin/castle...HEAD"], p)
    is_dirty = bool(status.get("stdout", "").strip()) if status.get("ok") else None
    behind = ahead = None
    if ahead_behind.get("ok") and ahead_behind.get("stdout"):
        parts = ahead_behind["stdout"].split()
        if len(parts) == 2:
            behind, ahead = int(parts[0]), int(parts[1])
    return {
        "exists": True,
        "branch": branch.get("stdout", "").strip() if branch.get("ok") else None,
        "is_dirty": is_dirty,
        "behind": behind,
        "ahead": ahead,
        "error": None if (branch.get("ok") and status.get("ok")) else status.get("stderr"),
    }


def diffstat(worktree_path: str, base_ref: str = "HEAD") -> dict:
    p = Path(worktree_path)
    return _run(["git", "diff", "--stat", base_ref], p)


def check_merged_status(
    worktree_or_branch: str,
    target_ref: str = "castle",
    base_ref: str = "castle",
    cwd: Optional[Path] = None,
) -> dict:
    """Deterministic merge verification and differencing against target_ref and base_ref."""
    root = get_repo_root(cwd)
    run_cwd = root
    worktree_path: Optional[Path] = None

    if Path(worktree_or_branch).exists() and Path(worktree_or_branch).is_dir():
        worktree_path = Path(worktree_or_branch)
        run_cwd = worktree_path
        branch_res = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], worktree_path)
        if branch_res.get("ok") and branch_res.get("stdout"):
            branch = branch_res["stdout"].strip()
        else:
            branch = worktree_path.name
    else:
        branch = worktree_or_branch.strip()
        found_wt = find_worktree_for_branch(branch, root)
        if found_wt:
            worktree_path = Path(found_wt)
            if not _run(["git", "rev-parse", "--verify", branch], root).get("ok") and not _run(["git", "rev-parse", "--verify", f"refs/heads/{branch}"], root).get("ok"):
                wt_branch_res = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], worktree_path)
                if wt_branch_res.get("ok") and wt_branch_res.get("stdout"):
                    branch = wt_branch_res["stdout"].strip()

    uncommitted_files: list[str] = []
    clean_worktree = True
    if worktree_path and worktree_path.exists() and worktree_path.is_dir():
        status_res = _run(["git", "status", "--porcelain=v1", "-uall"], worktree_path)
        if status_res.get("ok"):
            uncommitted_files = [line.strip() for line in status_res["stdout"].splitlines() if line.strip()]
            clean_worktree = len(uncommitted_files) == 0

    branch_ref = branch
    branch_verify = _run(["git", "rev-parse", "--verify", branch_ref], run_cwd)
    if not branch_verify.get("ok"):
        branch_ref = f"refs/heads/{branch}"
        branch_verify = _run(["git", "rev-parse", "--verify", branch_ref], run_cwd)

    if not branch_verify.get("ok"):
        found = False
        for b_name in (branch, f"quest/{branch}", f"epic/{branch}", f"scout/{branch}"):
            res = _run(["git", "rev-parse", "--verify", b_name], run_cwd)
            if res.get("ok"):
                branch_ref = b_name
                found = True
                break
            res2 = _run(["git", "rev-parse", "--verify", f"refs/heads/{b_name}"], run_cwd)
            if res2.get("ok"):
                branch_ref = f"refs/heads/{b_name}"
                found = True
                break
        if not found:
            return {
                "branch": branch,
                "target_ref": target_ref,
                "base_ref": base_ref,
                "is_merged": False,
                "clean_worktree": clean_worktree,
                "uncommitted_files": uncommitted_files,
                "recommendation": f"BRANCH_NOT_FOUND: git ref for '{branch}' could not be resolved.",
                "error": f"Branch ref not found: {branch}",
            }

    base_resolved = base_ref
    if not _run(["git", "rev-parse", "--verify", base_resolved], run_cwd).get("ok"):
        if _run(["git", "rev-parse", "--verify", f"refs/heads/{base_ref}"], run_cwd).get("ok"):
            base_resolved = f"refs/heads/{base_ref}"

    target_resolved = target_ref
    if not _run(["git", "rev-parse", "--verify", target_resolved], run_cwd).get("ok"):
        if _run(["git", "rev-parse", "--verify", f"refs/heads/{target_ref}"], run_cwd).get("ok"):
            target_resolved = f"refs/heads/{target_ref}"
        else:
            target_resolved = base_resolved

    unmerged_commits: list[dict] = []
    log_target = _run(["git", "log", "--oneline", f"{target_resolved}..{branch_ref}"], run_cwd)
    if log_target.get("ok") and log_target.get("stdout"):
        for line in log_target["stdout"].splitlines():
            h, _, msg = line.strip().partition(" ")
            unmerged_commits.append({"hash": h, "message": msg})

    unmerged_commits_base: list[dict] = []
    log_base = _run(["git", "log", "--oneline", f"{base_resolved}..{branch_ref}"], run_cwd)
    if log_base.get("ok") and log_base.get("stdout"):
        for line in log_base["stdout"].splitlines():
            h, _, msg = line.strip().partition(" ")
            unmerged_commits_base.append({"hash": h, "message": msg})

    diff_stat_res = _run(["git", "diff", "--stat", f"{target_resolved}...{branch_ref}"], run_cwd)
    diff_stat = diff_stat_res.get("stdout", "").strip()
    diff_quiet = _run(["git", "diff", "--quiet", f"{target_resolved}...{branch_ref}"], run_cwd)
    has_diff = (diff_quiet.get("exit_code") != 0)

    ancestor_target_res = _run(["git", "merge-base", "--is-ancestor", branch_ref, target_resolved], run_cwd)
    is_ancestor_target = (ancestor_target_res.get("exit_code") == 0)

    ancestor_base_res = _run(["git", "merge-base", "--is-ancestor", branch_ref, base_resolved], run_cwd)
    is_ancestor_base = (ancestor_base_res.get("exit_code") == 0)

    cherry_res = _run(["git", "cherry", target_resolved, branch_ref], run_cwd)
    cherry_unmerged = []
    cherry_merged = []
    if cherry_res.get("ok") and cherry_res.get("stdout"):
        for line in cherry_res["stdout"].splitlines():
            sign, _, sha = line.strip().partition(" ")
            if sign == "+":
                cherry_unmerged.append(sha)
            elif sign == "-":
                cherry_merged.append(sha)

    cherry_unmerged_count = len(cherry_unmerged)
    cherry_merged_count = len(cherry_merged)

    target_has_all_commits_heuristic = is_ancestor_target or (len(unmerged_commits) == 0) or (cherry_unmerged_count == 0 and len(unmerged_commits) > 0 and not has_diff)
    is_merged_target = is_ancestor_target and not has_diff

    target_in_base = (_run(["git", "merge-base", "--is-ancestor", target_resolved, base_resolved], run_cwd).get("exit_code") == 0)
    is_merged_base = is_ancestor_base or (is_merged_target and target_in_base and is_ancestor_target)

    is_merged = clean_worktree and is_merged_target

    if not clean_worktree:
        recommendation = (
            f"DIRTY_WORKTREE: {len(uncommitted_files)} uncommitted or untracked file(s) present in worktree; "
            "cannot safely prune until committed or cleaned."
        )
    elif is_merged:
        recommendation = (
            f"PRUNABLE: All commits and tree state are fully merged into {target_resolved}. "
            "Safe to prune branch and remove worktree."
        )
    elif is_merged_base and not is_merged_target:
        recommendation = (
            f"MERGED_INTO_BASE: Merged into base ({base_resolved}) though not in target ({target_resolved}). "
            "Safe to prune if base is authoritative."
        )
    elif len(unmerged_commits) > 0 and cherry_unmerged_count == 0 and not has_diff:
        recommendation = (
            f"REBASED_EQUIVALENT: Branch commits appear to have been rebased or cherry-picked into {target_resolved} "
            "(tree diff is empty). Safe to prune if confirmed."
        )
    elif len(unmerged_commits) > 0:
        recommendation = (
            f"UNMERGED_COMMITS: {len(unmerged_commits)} commit(s) on branch not reachable in {target_resolved}. "
            "Do not prune until merged or explicitly discarded."
        )
    elif has_diff:
        recommendation = (
            f"TREE_DIFF: Branch has differences vs {target_resolved} despite commit ancestry. "
            "Review diff before pruning."
        )
    else:
        recommendation = "UNVERIFIED: Status could not be verified automatically."

    return {
        "branch": branch,
        "resolved_branch_ref": branch_ref,
        "target_ref": target_resolved,
        "base_ref": base_resolved,
        "is_merged": is_merged,
        "is_merged_target": is_merged_target,
        "is_merged_base": is_merged_base,
        "is_ancestor_target": is_ancestor_target,
        "is_ancestor_base": is_ancestor_base,
        "clean_worktree": clean_worktree,
        "uncommitted_files": uncommitted_files,
        "unmerged_commits": unmerged_commits,
        "unmerged_commits_count": len(unmerged_commits),
        "unmerged_commits_base_count": len(unmerged_commits_base),
        "has_diff": has_diff,
        "diff_stat": diff_stat,
        "cherry_unmerged_count": cherry_unmerged_count,
        "cherry_merged_count": cherry_merged_count,
        "cherry_unmerged_commits": cherry_unmerged,
        "cherry_merged_commits": cherry_merged,
        "target_has_all_commits_heuristic": target_has_all_commits_heuristic,
        "recommendation": recommendation,
        "ok": True,
    }


def is_quest_merged_into(
    quest,
    target_ref: str = "main",
    cwd: Optional[Path] = None,
) -> bool:
    """Return True if the quest's deliverables have already been merged into target_ref.
    Returns False for scouts/investigation spikes (non-merging spikes) or if target_ref
    cannot be resolved.
    """
    if getattr(quest, "kind", "") == "scout" or getattr(quest, "section", "") == "Investigation":
        return False
    root = get_repo_root(cwd)
    ref_check = _run(["git", "rev-parse", "--verify", target_ref], root)
    if not ref_check.get("ok"):
        ref_check = _run(["git", "rev-parse", "--verify", f"refs/heads/{target_ref}"], root)
        if not ref_check.get("ok"):
            return False

    promoted = getattr(quest, "cogship_promoted_commit", None)
    if promoted:
        c_check = _run(["git", "merge-base", "--is-ancestor", str(promoted).strip(), target_ref], root)
        if c_check.get("exit_code") == 0:
            return True

    branch = getattr(quest, "branch", None)
    if branch:
        st = check_merged_status(branch, target_ref=target_ref, base_ref=target_ref, cwd=root)
        if st.get("is_merged_target") or st.get("is_ancestor_target"):
            return True

        # Post-promotion paperwork drift (cogship-257/261 staleness): quest
        # branches keep receiving .court/ bookkeeping commits after the
        # convoy's code was merged, so branch-tip ancestry alone reports
        # sailed convoys as unmerged and resurrects deploys that already
        # shipped as "ready to confirm". Deliverable-level fallback: when the
        # branch's unique changes relative to target_ref touch only court
        # paperwork paths (.court/), the production deliverables are already
        # merged; the outstanding paperwork rides the trunk deploy backlog.
        diff_res = _run(["git", "diff", "--name-only", f"{target_ref}...{branch}"], root)
        if diff_res.get("ok"):
            files = [f.strip() for f in (diff_res.get("stdout") or "").splitlines() if f.strip()]
            if all(f.startswith(".court/") for f in files):
                return True

    return False


def run_test_command(worktree_path: str, test_cmd: str, timeout: int = 600) -> dict:
    p = Path(worktree_path)
    if not p.exists():
        return {"ok": False, "error": f"path does not exist: {worktree_path}"}
    result = _run(["/bin/sh", "-c", test_cmd], p, timeout=timeout)
    for k in ("stdout", "stderr"):
        if len(result.get(k, "")) > 4000:
            result[k] = "...(truncated)...\n" + result[k][-4000:]
    return result


# Court-wide write lock (castle-contention fix, 2026-09-13): every mutating
# court operation (advance, collect, coin sync-back, raze, stamp...) shares one
# checkout of the repo root. Concurrent writers collided on git's index.lock
# and on each other's dirty tree. This advisory flock serializes them cleanly;
# read paths never take it.
_COURT_LOCK_NAME = ".court/.write.lock"
_COURT_LOCK_TIMEOUT = 180
_lock_depth = 0
_lock_depth_guard = threading.Lock()


@contextmanager
def court_write_lock(timeout: int = _COURT_LOCK_TIMEOUT, cwd: Optional[Path | str] = None):
    """Exclusive advisory lock over all castle paperwork mutations. Re-entrant
    within a process (nested callers no-op) so a held batch lock can safely
    wrap inner git_commit_paths calls."""
    global _lock_depth
    with _lock_depth_guard:
        _lock_depth += 1
        outermost = _lock_depth == 1
    f = None
    try:
        if outermost:
            root = get_repo_root(cwd)
            lock_path = root / _COURT_LOCK_NAME
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            f = open(lock_path, "a+")
            deadline = time.time() + timeout
            while True:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise TimeoutError(
                            f"court write lock still held by another process after {timeout}s "
                            f"({lock_path}) — another Court role is mid-write; retry shortly"
                        )
                    time.sleep(0.1)
        yield
    finally:
        if f is not None:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            finally:
                f.close()
        with _lock_depth_guard:
            _lock_depth -= 1


def can_fast_forward(worktree_path: str, base_branch: str = "castle") -> dict:
    p = Path(worktree_path)
    fetch = _run(["git", "fetch", "origin", base_branch], p, timeout=60)
    behind_check = _run(
        ["git", "rev-list", "--count", f"HEAD..origin/{base_branch}"], p
    )
    behind = None
    if behind_check.get("ok") and behind_check.get("stdout").isdigit():
        behind = int(behind_check["stdout"])
    return {"fetch_ok": fetch.get("ok"), "behind_base": behind}


def rebase_worktree_onto_base(
    worktree_path: str | Path,
    base_branch: str = "castle",
    auto_resolve_foreign_ledger: bool = True,
    own_quest_id: Optional[str] = None,
) -> dict:
    """Mechanically converge a worktree onto `base_branch` via `git merge <base> --no-edit`."""
    p = Path(worktree_path)
    result: dict = {
        "ok": False,
        "merged": False,
        "already_current": False,
        "conflict": False,
        "auto_resolved_files": [],
        "conflicting_files": [],
        "error": None,
    }
    if not p.exists() or not p.is_dir():
        result["error"] = f"worktree path does not exist: {worktree_path}"
        return result

    status_res = _run(["git", "status", "--porcelain=v1", "-uall"], p)
    if not status_res.get("ok"):
        result["error"] = f"git status failed: {status_res.get('stderr')}"
        return result
    dirty_lines = [l for l in status_res.get("stdout", "").splitlines() if l.strip()]
    if dirty_lines:
        result["error"] = (
            f"worktree is dirty ({len(dirty_lines)} uncommitted file(s)); "
            "must be cleaned by Serf before rebase"
        )
        return result

    behind_res = _run(["git", "rev-list", "--count", f"HEAD..{base_branch}"], p)
    before_behind = None
    if behind_res.get("ok") and behind_res.get("stdout", "").strip().isdigit():
        before_behind = int(behind_res["stdout"].strip())
    result["before_behind"] = before_behind
    if behind_res.get("ok") and behind_res.get("stdout", "").strip() == "0":
        result["ok"] = True
        result["already_current"] = True
        return result

    merge_res = _run(["git", "merge", base_branch, "--no-edit"], p, timeout=120)
    if merge_res.get("ok"):
        result["ok"] = True
        result["merged"] = True
        result["after_behind"] = 0
        return result

    conflict_res = _run(["git", "diff", "--name-only", "--diff-filter=U"], p)
    conflict_files = [
        line.strip() for line in conflict_res.get("stdout", "").splitlines() if line.strip()
    ]
    result["conflicting_files"] = conflict_files

    if auto_resolve_foreign_ledger and conflict_files:
        own_id = (own_quest_id or "").upper().split("-")[0]
        auto_resolved: list[str] = []
        remaining_conflicts: list[str] = []

        for f in conflict_files:
            norm = f.replace("\\", "/")
            is_foreign_ledger = (
                (norm.startswith(".court/quests/") or norm.startswith(".court/epics/"))
                and norm.endswith(".md")
                and (not own_id or own_id not in Path(f).stem.upper())
            )
            if is_foreign_ledger:
                chk_res = _run(["git", "checkout", f"--theirs", f], p)
                add_res = _run(["git", "add", f], p)
                if chk_res.get("ok") and add_res.get("ok"):
                    auto_resolved.append(f)
                else:
                    remaining_conflicts.append(f)
            else:
                remaining_conflicts.append(f)

        result["auto_resolved_files"].extend(auto_resolved)

        if auto_resolved and not remaining_conflicts:
            unmerged_check = _run(["git", "diff", "--name-only", "--diff-filter=U"], p)
            if not unmerged_check.get("stdout", "").strip():
                commit_res = _run(["git", "commit", "--no-edit"], p, timeout=60)
                if commit_res.get("ok"):
                    result["ok"] = True
                    result["merged"] = True
                    result["conflict"] = False
                    result["conflicting_files"] = []
                    result["after_behind"] = 0
                    return result
        conflict_files = remaining_conflicts

    if conflict_files and own_quest_id:
        own_id = own_quest_id.upper().split("-")[0]
        reconciled_any = False
        remaining_after_union: list[str] = []

        for f in conflict_files:
            norm = f.replace("\\", "/")
            is_own_ledger = (
                (norm.startswith(".court/quests/") or norm.startswith(".court/epics/"))
                and norm.endswith(".md")
                and own_id in Path(f).stem.upper()
            )
            if is_own_ledger and _reconcile_history_only_conflict(p / f):
                add_res = _run(["git", "add", f], p)
                if add_res.get("ok"):
                    result["auto_resolved_files"].append(f + " (ledger union)")
                    reconciled_any = True
                else:
                    remaining_after_union.append(f)
            else:
                remaining_after_union.append(f)

        if reconciled_any and not remaining_after_union:
            unmerged_check = _run(["git", "diff", "--name-only", "--diff-filter=U"], p)
            if not unmerged_check.get("stdout", "").strip():
                commit_res = _run(["git", "commit", "--no-edit"], p, timeout=60)
                if commit_res.get("ok"):
                    result["ok"] = True
                    result["merged"] = True
                    result["conflict"] = False
                    result["conflicting_files"] = []
                    result["after_behind"] = 0
                    return result
        conflict_files = remaining_after_union

    _run(["git", "merge", "--abort"], p)
    result["conflict"] = True
    result["error"] = merge_res.get("stderr") or merge_res.get("stdout") or "git merge failed"
    return result


def get_branch_diffstat(base: str = "main", head: str = "castle", cwd: Optional[Path] = None) -> dict:
    p = cwd or Path.cwd()
    return _run(["git", "diff", "--stat", f"{base}..{head}"], p)


def get_branch_log(base: str = "main", head: str = "castle", max_count: int = 50, cwd: Optional[Path] = None) -> dict:
    p = cwd or Path.cwd()
    return _run(["git", "log", f"{base}..{head}", "--oneline", f"-n{max_count}"], p)


def get_ahead_behind(base: str = "main", head: str = "castle", cwd: Optional[Path] = None) -> dict:
    p = cwd or Path.cwd()
    result = _run(["git", "rev-list", "--left-right", "--count", f"{base}...{head}"], p)
    behind = ahead = None
    if result.get("ok") and result.get("stdout"):
        parts = result["stdout"].split()
        if len(parts) == 2:
            behind, ahead = int(parts[0]), int(parts[1])
    return {"ok": result.get("ok"), "base": base, "head": head, "behind": behind, "ahead": ahead}


def git_commit_paths(
    paths: list[Path | str] | Path | str,
    commit_msg: str,
    cwd: Optional[Path] = None,
) -> dict:
    """Stage and commit specific file paths atomically without touching other files."""
    p = cwd or get_repo_root()
    with court_write_lock(cwd=p):
        if isinstance(paths, (str, Path)):
            path_list = [Path(paths)]
        else:
            path_list = [Path(x) for x in paths]

        diff_before = _run(["git", "diff", "--cached", "--name-only"], p)
        staged_prior = [
            line.strip() for line in diff_before.get("stdout", "").splitlines() if line.strip()
        ] if diff_before.get("ok") else []

        unstage_needed = bool(staged_prior)

        rel_paths = []
        for target in path_list:
            try:
                rel = target.relative_to(p)
                rel_paths.append(str(rel))
            except ValueError:
                rel_paths.append(str(target))

        add_res = _run(["git", "add", "--"] + rel_paths, p)
        if not add_res.get("ok"):
            return {
                "ok": False,
                "exit_code": add_res.get("exit_code"),
                "stdout": add_res.get("stdout", ""),
                "stderr": add_res.get("stderr", ""),
                "cmd": add_res.get("cmd", ""),
            }

        commit_res = _run(["git", "commit", "-m", commit_msg, "--"] + rel_paths, p)
        out_stdout = commit_res.get("stdout", "")
        out_stderr = commit_res.get("stderr", "")

        if unstage_needed and commit_res.get("ok"):
            _run(["git", "restore", "--staged", "."], p)

        is_no_changes = (
            "nothing to commit" in out_stdout.lower()
            or "nothing to commit" in out_stderr.lower()
            or "no changes added to commit" in out_stdout.lower()
            or "no changes added to commit" in out_stderr.lower()
        )

        return {
            "ok": commit_res.get("ok") or is_no_changes,
            "no_changes": is_no_changes,
            "exit_code": commit_res.get("exit_code"),
            "stdout": out_stdout,
            "stderr": out_stderr,
            "cmd": commit_res.get("cmd", ""),
        }


def check_proof_of_landing(quest: Any, cwd: Optional[Path | str] = None) -> dict:
    root = get_repo_root(cwd)
    quest_id = getattr(quest, "id", str(quest))
    branch = getattr(quest, "branch", "")
    concern = getattr(quest, "concern", "")
    short_id = quest_id.split("-")[0].upper()

    target_branches = ["castle", "main"]
    for s in ("north", "south", "east", "west"):
        target_branches.append(f"the-gatehouse/{s}")

    found_proofs: list[dict] = []

    if branch:
        for tb in target_branches:
            anc = _run(["git", "merge-base", "--is-ancestor", branch, tb], root)
            if anc.get("exit_code") == 0:
                found_proofs.append({
                    "target": tb,
                    "kind": "ancestor",
                    "detail": f"branch '{branch}' is an ancestor of '{tb}'",
                })

    for tb in target_branches:
        if not _run(["git", "rev-parse", "--verify", f"refs/heads/{tb}"], root).get("ok"):
            continue

        log_id = _run(["git", "log", f"-n100", f"--grep={short_id}", "--oneline", tb], root)
        if log_id.get("ok") and log_id.get("stdout"):
            for line in log_id["stdout"].splitlines():
                found_proofs.append({
                    "target": tb,
                    "kind": "commit_msg_id",
                    "detail": line.strip(),
                })

        if concern and len(concern) >= 4:
            log_c = _run(["git", "log", f"-n100", f"--grep={concern}", "--oneline", tb], root)
            if log_c.get("ok") and log_c.get("stdout"):
                for line in log_c["stdout"].splitlines():
                    if line.strip() not in [p.get("detail") for p in found_proofs]:
                        found_proofs.append({
                            "target": tb,
                            "kind": "commit_msg_concern",
                            "detail": line.strip(),
                        })

    return {
        "quest_id": quest_id,
        "has_proof": len(found_proofs) > 0,
        "proofs": found_proofs,
        "proof_count": len(found_proofs),
    }


def resolve_branch_tip(branch: str, cwd: Optional[Path | str] = None) -> Optional[str]:
    tip = _run(["git", "rev-parse", "--verify", f"refs/heads/{branch}"], get_repo_root(cwd))
    if tip.get("ok") and tip.get("stdout"):
        return tip["stdout"].strip()
    return None


_PRODUCTION_FILE_EXCLUDES = (".court/", "tasks/")


def verify_quest_promotion(
    branch: str,
    trunk: str = "castle",
    require_code: bool = True,
    cwd: Optional[Path | str] = None,
) -> dict:
    """cogship-076/077 promotion-integrity check (three-layer incident, 2026-09-13).

    Verifies the promotion claim for one Quest's branch, direction-aware:
      1. BRANCH-TIP ancestry: the quest branch TIP itself (not an arbitrary
         --verified-commit) must be an ancestor of the trunk. Cogship-077's
         partially-packed convoy passed the old check because *some* commit
         was reachable from castle while three manifest branches were never
         merged at all.
      2. Production-content presence on the branch: the branch's diff vs its
         merge-base with the trunk must contain at least one non-bookkeeping
         file (anything outside .court/ and tasks/). Cogship-076's "promotion"
         was a backwards merge (castle -> convoy) whose diffstat was pure
         .court/ bookkeeping — the old ancestry check passed mechanically.

    Returns {ok, branch, tip, is_ancestor, production_files, bookkeeping_files, reason}.
    """
    result: dict = {
        "ok": False, "branch": branch, "tip": None, "is_ancestor": False,
        "production_files": [], "bookkeeping_files": [], "reason": None,
    }
    root = get_repo_root(cwd)
    tip = resolve_branch_tip(branch, root)
    if not tip:
        result["reason"] = f"branch '{branch}' does not resolve — cannot verify any promotion claim"
        return result
    result["tip"] = tip

    anc = _run(["git", "merge-base", "--is-ancestor", tip, trunk], root)
    result["is_ancestor"] = anc.get("exit_code") == 0
    if not result["is_ancestor"]:
        result["reason"] = (
            f"branch tip {tip[:12]} is NOT an ancestor of {trunk} — the branch was never "
            f"promoted (a backwards castle->convoy merge or a partial pack leaves it outside)"
        )
        return result

    if require_code:
        # Branch-unique commits (by patch-id): '+' means the patch is NOT in
        # trunk, '-' means an equivalent patch is present there.
        cherry = _run(["git", "cherry", trunk, tip], root)
        lines = [l for l in cherry.get("stdout", "").splitlines() if l.strip()]
        plus = [l[1:].strip() for l in lines if l.startswith("+")]
        minus = [l[1:].strip() for l in lines if l.startswith("-")]

        if plus:
            # Not patch-equivalent in trunk. Allow the squash-merge case: the
            # squashed commit's patch-id differs, but if the files the branch
            # touches are content-identical in trunk, the code did land.
            plus_files_res = _run(["git", "log", "--name-only", "--pretty=format:", *plus], root)
            plus_files = sorted({l.strip() for l in plus_files_res.get("stdout", "").splitlines() if l.strip()})
            if plus_files:
                same_res = _run(["git", "diff", "--quiet", tip, trunk, "--", *plus_files], root)
                if same_res.get("exit_code") != 0:
                    result["reason"] = (
                        f"{len(plus)} branch commit(s) are NOT in {trunk} (not patch-equivalent, "
                        f"files differ) — the branch was never promoted: {', '.join(c[:12] for c in plus[:3])}"
                    )
                    return result
            else:
                result["reason"] = f"{len(plus)} branch commit(s) are NOT in {trunk} — the branch was never promoted"
                return result

        # Production-content presence among the branch's own unique commits.
        if minus:
            files_res = _run(["git", "log", "--name-only", "--pretty=format:", *minus], root)
            files = [l.strip() for l in files_res.get("stdout", "").splitlines() if l.strip()]
        else:
            # Shas preserved by the promotion (merge --no-ff or fast-forward):
            # locate the promotion merge commit (second parent == tip) and use
            # its first-parent diffstat; a pure fast-forward has none.
            merges = _run(
                ["git", "log", "--merges", "--ancestry-path", "--pretty=format:%H %P", f"{tip}..{trunk}"],
                root,
            )
            promo_sha = None
            for line in merges.get("stdout", "").splitlines():
                parts = line.strip().split()
                if len(parts) == 3 and parts[2] == tip:
                    promo_sha = parts[0]
                    break
            if promo_sha:
                diff_res = _run(["git", "diff", "--name-only", f"{promo_sha}^1", promo_sha], root)
                files = [l.strip() for l in diff_res.get("stdout", "").splitlines() if l.strip()]
            else:
                # Fast-forward promotion: ancestry already proves content
                # presence in trunk; nothing further to verify.
                result["ok"] = True
                return result

        result["bookkeeping_files"] = [f for f in files if f.startswith(_PRODUCTION_FILE_EXCLUDES)]
        result["production_files"] = [f for f in files if not f.startswith(_PRODUCTION_FILE_EXCLUDES)]
        if not result["production_files"]:
            result["reason"] = (
                f"branch tip {tip[:12]} is reachable from {trunk} but carries ZERO production "
                f"files (only bookkeeping: {', '.join(result['bookkeeping_files'][:5]) or 'none'}) "
                "— this is bookkeeping drift, not a code promotion"
            )
            return result

    result["ok"] = True
    return result


def create_git_worktree(
    worktree_path: str | Path,
    branch: str,
    base_branch: str = "castle",
    cwd: Optional[Path | str] = None,
) -> dict:
    """Create a new git worktree on the given branch (cut from base_branch if branch is new)."""
    root = get_repo_root(cwd)
    wt_p = Path(worktree_path)
    if not wt_p.is_absolute():
        wt_p = root / wt_p

    res_b = _run(["git", "rev-parse", "--verify", branch], root)
    if res_b.get("ok"):
        cmd = ["git", "worktree", "add", str(wt_p), branch]
    else:
        base_ref = base_branch if _run(["git", "rev-parse", "--verify", base_branch], root).get("ok") else "HEAD"
        cmd = ["git", "worktree", "add", "-b", branch, str(wt_p), base_ref]

    res = _run(cmd, root)
    clear_git_cache()
    return {
        "ok": res.get("ok", False),
        "path": str(wt_p),
        "branch": branch,
        "output": (res.get("stdout", "") or res.get("stderr", "")).strip(),
    }


def check_charter_integrity(quest: Any, worktree_path: str | Path, base: str = "castle") -> dict:
    """Verify that the worktree branch has not modified/tampered with The Kingdom Requires
    or Expected Tribute text relative to base (e.g. castle).

    Returns dict with:
      ok: bool
      tampered: bool
      violations: list[str]
      base_file_found: bool
    """
    from .models import Quest

    p = Path(worktree_path)
    if not p.exists() or not p.is_dir():
        return {"ok": False, "tampered": False, "violations": [], "base_file_found": False}

    quest_id = getattr(quest, "id", str(quest))
    kind = getattr(quest, "kind", "quest")
    subdir = "epics" if kind == "epic" else "quests"
    candidate_rel_paths = [
        f".court/{subdir}/{quest_id}.md",
        f".court/archive/{quest_id}.md",
    ]

    base_text = None
    for rel_path in candidate_rel_paths:
        res = _run(["git", "show", f"{base}:{rel_path}"], p)
        if res.get("ok") and res.get("stdout"):
            base_text = res["stdout"]
            break

    if not base_text:
        return {"ok": True, "tampered": False, "violations": [], "base_file_found": False}

    try:
        base_quest = Quest.from_markdown(base_text)
    except Exception:
        return {"ok": True, "tampered": False, "violations": [], "base_file_found": True}

    violations = []

    # 1. Compare The Kingdom Requires / Goal & Scope
    def _norm_text(t: str) -> str:
        return "\n".join(line.strip() for line in t.splitlines() if line.strip())

    base_goal = (base_quest.body_sections.get("The Kingdom Requires") or base_quest.body_sections.get("Goal & Scope") or "").strip()
    head_goal = (getattr(quest, "body_sections", {}).get("The Kingdom Requires") or getattr(quest, "body_sections", {}).get("Goal & Scope") or "").strip()

    if base_goal and head_goal and _norm_text(base_goal) != _norm_text(head_goal):
        violations.append(
            f"Charter tampering detected: '# The Kingdom Requires' text was modified on branch relative to {base} baseline."
        )

    # 2. Compare Expected Tribute checklist items (ignoring [ ] vs [x])
    def _extract_checklist_items(text: str) -> list[str]:
        items = []
        for line in text.splitlines():
            m = re.match(r"^\s*[-*+]\s+\[[ xX~-]\]\s+(.+)$", line)
            if m:
                items.append(m.group(1).strip())
        return items

    base_items = _extract_checklist_items(base_quest.body_sections.get("Expected Tribute", ""))
    head_items = _extract_checklist_items(getattr(quest, "body_sections", {}).get("Expected Tribute", ""))

    if base_items and base_items != head_items:
        violations.append(
            f"Charter tampering detected: '# Expected Tribute' checklist items were modified/added/removed on branch relative to {base} baseline."
        )

    tampered = len(violations) > 0
    return {
        "ok": True,
        "tampered": tampered,
        "violations": violations,
        "base_file_found": True,
    }


_SUITE_PROOF_DIRNAME = ".court/suites"

# Q377/cogship-082 (5th falsified-Gatekeeper incident): the Gatekeeper agent
# claimed a unified suite pass that never ran — every attempt died on its
# shell tool's 120-second timeout — and nothing engine-side could tell the
# difference between a real run and quoted Tribute text. The suite must now
# be executed BY the engine (`court runsuite`), which stamps a durable proof
# file (command, HEAD sha, exit code, test counts, output tail) that the
# READY_TO_RAZE gate independently re-verifies.

_PYTEST_COUNT_RE = re.compile(r"(?<![\w.])(\d+)\s+passed(?:[,\s]|$)", re.MULTILINE)
_DJANGO_RAN_RE = re.compile(r"Ran\s+(\d+)\s+tests?")

# Stale kept test DBs (Q620 class): a migration renumber (0038 -> 0039) makes a
# --keepdb/--reuse-db database fail with "column already exists" / duplicate
# table errors, because the kept DB recorded the OLD migration set. The fix is
# to fingerprint the migrations tree per worktree and, when it changes, run the
# suite ONCE with the DB-reuse flag dropped (Django --keepdb) or a forced
# recreate added (pytest-django --create-db) — that run rebuilds the test DB
# from the current migrations, and subsequent runs may keep it again.
_KEEPDB_FP_FILENAME = ".testdb-fingerprint.json"
_MIGRATIONS_SCAN_SKIP_DIRS = {
    ".git", ".kilo", ".court", "node_modules", "venv", ".venv", "env", "__pycache__",
}


def suite_keepdb_mode(cmd_str: str) -> Optional[str]:
    """Classify a suite command's test-DB reuse flag: "django" for --keepdb,
    "pytest" for pytest-django --reuse-db, None when the DB is rebuilt every
    run (nothing to go stale)."""
    tokens = cmd_str.split()
    if "--keepdb" in tokens:
        return "django"
    if "--reuse-db" in tokens:
        return "pytest"
    return None


def keepdb_recreate_command(cmd_str: str, mode: str) -> str:
    """Return the suite command variant that DESTROYS + rebuilds the test DB:
    Django loses --keepdb (a plain test run recreates and then removes the DB),
    pytest-django gains --create-db (overrides --reuse-db for that one run)."""
    tokens = [t for t in cmd_str.split() if not (mode == "django" and t == "--keepdb")]
    if mode == "pytest" and "--create-db" not in tokens:
        tokens.append("--create-db")
    return " ".join(tokens)


def migrations_fingerprint(worktree_path: Path) -> str:
    """Content fingerprint of every migrations/*.py file in a checkout. A
    migration renumber changes filenames and contents alike, so any renumber
    (or edit) produces a different fingerprint."""
    import hashlib

    p = Path(worktree_path)
    entries: list[str] = []
    for mig_dir in sorted(p.rglob("migrations")):
        if not mig_dir.is_dir():
            continue
        rel_parent = mig_dir.parent.relative_to(p)
        if any(part in _MIGRATIONS_SCAN_SKIP_DIRS for part in mig_dir.parts):
            continue
        for f in sorted(mig_dir.glob("*.py")):
            try:
                content = f.read_bytes()
            except OSError:
                continue
            entries.append(f"{rel_parent / f.name}:{len(content)}:{hashlib.sha256(content).hexdigest()}")
    if not entries:
        return ""
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def keepdb_recreate_decision(
    cmd_str: str,
    fingerprint: str,
    stored: Optional[dict],
) -> tuple[bool, str, str]:
    """Decide whether the kept test DB must be rebuilt before this suite run.

    Returns (recreate, run_command, reason). Recreate when a stored fingerprint
    exists, the DB-reuse mode matches, and the migrations fingerprint changed
    (or the mode itself changed). No stored fingerprint means nothing has gone
    stale yet — keep the DB and start recording."""
    mode = suite_keepdb_mode(cmd_str)
    if mode is None:
        return False, cmd_str, ""
    if not stored:
        return False, cmd_str, ""
    if (stored.get("mode") or "") != mode:
        return True, keepdb_recreate_command(cmd_str, mode), f"DB-reuse mode changed to {mode}"
    if fingerprint and (stored.get("fingerprint") or "") != fingerprint:
        return True, keepdb_recreate_command(cmd_str, mode), (
            "migrations tree changed since the kept DB was built (renumber/edit)"
        )
    return False, cmd_str, ""


def _keepdb_fingerprint_path(worktree_path: Path) -> Path:
    return Path(worktree_path) / _SUITE_PROOF_DIRNAME / _KEEPDB_FP_FILENAME


def _suite_proof_path(cwd: Optional[Path | str] = None, cogship_id: str = "", quest_id: str = "") -> Optional[Path]:
    root = get_repo_root(cwd)
    name = (cogship_id or quest_id or "").strip()
    if not name:
        return None
    return root / _SUITE_PROOF_DIRNAME / f"{name}.json"


def _detect_suite_command(worktree_path: Path) -> str:
    # Q455: the canonical unified integration suite command lives in the
    # manifest (.court/config.json `suite.command` — the same value stamped
    # into the cogship suite proofs). The manage.py/pytest probe below is only
    # a fallback for castles whose manifest predates the suite block.
    manifest_cmd = get_suite_command()
    if manifest_cmd:
        return manifest_cmd
    if (worktree_path / "manage.py").exists():
        return "python3 manage.py test"
    return "python3 -m pytest -q"


def run_unified_suite(
    worktree_path: str | Path,
    command: Optional[str] = None,
    cogship_id: str = "",
    quest_id: str = "",
    timeout: int = 1800,
    cwd: Optional[Path | str] = None,
) -> dict:
    """Run the unified integration suite in a worktree via the ENGINE (never an
    agent's shell tool, whose short timeout is what falsified cogship-082) and
    stamp durable proof to `.court/suites/<cogship-or-quest>.json`.

    Q432: the suite's stage 1 is the DB-free migration-graph check
    (`ROLE=web python manage.py makemigrations --check --dry-run`). Its
    verdict is stamped as `migration_graph_ok` into the proof JSON, and a
    failure makes the suite exit non-zero (exit code 2, vs the test
    command's own code on test failures) — the 2026-09-14 v1383 deploy
    failed on a same-parent migration fork invisible to git and to
    per-quest test suites, so the graph must gate here too. On a graph
    failure the expensive test battery is skipped (fail fast): a broken
    graph is terminal for the convoy and its own test noise adds nothing.
    """
    p = Path(worktree_path)
    result: dict = {
        "ok": False,
        "exit_code": None,
        "proof_path": None,
        "error": None,
        "migration_graph_ok": None,
    }
    if not p.exists() or not p.is_dir():
        result["error"] = f"worktree path does not exist: {worktree_path}"
        return result

    head_res = _run(["git", "rev-parse", "HEAD"], p)
    head_sha = head_res.get("stdout", "").strip()

    started = datetime.now(timezone.utc).isoformat()

    # Stage 1 — migration-graph integrity (DB-free, seconds). Only applies to
    # Django checkouts (manage.py present); a non-Django castle has no
    # migration graph to verify, so the stage is not applicable there.
    if (p / "manage.py").is_file():
        graph = migration_graph.check_migration_graph(p)
        migration_graph_ok = bool(graph.get("ok"))
        result["migration_graph_ok"] = migration_graph_ok

        if not migration_graph_ok:
            finished = datetime.now(timezone.utc).isoformat()
            graph_reason = graph.get("reason") or "unknown graph failure"
            proof = {
                "cogship_id": cogship_id or "",
                "quest_id": quest_id or "",
                "worktree": str(p),
                "command": graph.get("command", migration_graph.MIGRATION_GRAPH_SHELL_COMMAND),
                "head_sha": head_sha,
                "exit_code": 2,
                "ran_tests": None,
                "migration_graph_ok": False,
                "migration_graph_mode": graph.get("mode"),
                "migration_graph_reason": graph_reason,
                "started": started,
                "finished": finished,
                "output_tail": graph.get("output_tail", "")[-4000:],
            }
            proof_path = _suite_proof_path(cwd, cogship_id=cogship_id, quest_id=quest_id)
            if proof_path:
                proof_path.parent.mkdir(parents=True, exist_ok=True)
                proof_path.write_text(json.dumps(proof, indent=2), encoding="utf-8")
                result["proof_path"] = str(proof_path)
            result.update({
                "ok": False,
                "exit_code": 2,
                "ran_tests": None,
                "head_sha": head_sha,
                "command": proof["command"],
                "error": f"migration graph preflight failed: {graph_reason}",
            })
            return result
    else:
        graph = {"mode": "not_applicable"}
        result["migration_graph_ok"] = None

    # Stage 2 — the test battery itself. When the suite keeps its test DB
    # (--keepdb / --reuse-db), a migration renumber since the DB was built
    # makes it fail with "column already exists"; rebuild it once instead.
    cmd_str = command or _detect_suite_command(p)
    test_db_recreated = ""
    keepdb_mode = suite_keepdb_mode(cmd_str)
    fp_path = _keepdb_fingerprint_path(p)
    migrations_fp = ""
    if keepdb_mode:
        migrations_fp = migrations_fingerprint(p)
        stored = None
        try:
            stored = json.loads(fp_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = None
        recreate, cmd_str, reason = keepdb_recreate_decision(cmd_str, migrations_fp, stored)
        if recreate:
            test_db_recreated = reason
    argv = cmd_str.split()
    try:
        proc = subprocess.run(argv, cwd=str(p), capture_output=True, text=True, timeout=timeout)
        exit_code = proc.returncode
        output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    except subprocess.TimeoutExpired as e:
        exit_code = -1
        output = f"suite run exceeded engine timeout ({timeout}s)"
        if isinstance(e.stdout, str):
            output += "\n" + e.stdout
    except FileNotFoundError:
        result["error"] = f"suite command not found: {argv[0]}"
        return result
    finished = datetime.now(timezone.utc).isoformat()

    if keepdb_mode:
        # Record the fingerprint AFTER the run: on a recreate run the kept DB
        # now matches these migrations; on a normal run nothing changed.
        try:
            fp_path.parent.mkdir(parents=True, exist_ok=True)
            fp_path.write_text(
                json.dumps({"fingerprint": migrations_fp, "mode": keepdb_mode, "finished": finished}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    m_pytest = _PYTEST_COUNT_RE.search(output)
    m_dj = _DJANGO_RAN_RE.search(output)
    ran_tests = int(m_pytest.group(1)) if m_pytest else (int(m_dj.group(1)) if m_dj else None)

    proof = {
        "cogship_id": cogship_id or "",
        "quest_id": quest_id or "",
        "worktree": str(p),
        "command": cmd_str,
        "head_sha": head_sha,
        "exit_code": exit_code,
        "ran_tests": ran_tests,
        "migration_graph_ok": result["migration_graph_ok"],
        "migration_graph_mode": graph.get("mode"),
        "test_db_recreated": test_db_recreated,
        "started": started,
        "finished": finished,
        "output_tail": output[-4000:],
    }

    proof_path = _suite_proof_path(cwd, cogship_id=cogship_id, quest_id=quest_id)
    if proof_path:
        proof_path.parent.mkdir(parents=True, exist_ok=True)
        proof_path.write_text(json.dumps(proof, indent=2), encoding="utf-8")
        result["proof_path"] = str(proof_path)

    result.update({
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "ran_tests": ran_tests,
        "head_sha": head_sha,
        "command": cmd_str,
        "test_db_recreated": test_db_recreated,
    })
    return result


def check_suite_proof(
    cogship_id: str = "",
    quest_id: str = "",
    head_sha: str = "",
    cwd: Optional[Path | str] = None,
) -> dict:
    """Independently verify a stamped suite proof: exit code 0, a real test
    count, and a head_sha that is either the branch tip being promoted or an
    ancestor of `castle` (i.e. the run covered the code that actually landed)."""
    fail = {"ok": False, "valid": False, "reason": None, "proof": None}
    candidates = []
    for name in (cogship_id, quest_id):
        name = (name or "").strip()
        if name:
            proof_path = _suite_proof_path(cwd, cogship_id="", quest_id=name)
            if proof_path and proof_path.exists():
                candidates.append(proof_path)
    if not candidates:
        fail["reason"] = (
            f"no engine-stamped suite proof at {get_repo_root(cwd) / _SUITE_PROOF_DIRNAME}/"
            f"<{cogship_id or quest_id}.json> — the unified suite was never run via "
            "`court runsuite`; an agent's claim of a pass is not proof"
        )
        return fail
    proof_path = candidates[0]
    try:
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
    except Exception as e:
        fail["reason"] = f"suite proof unreadable/corrupt: {e}"
        return fail
    fail["proof"] = {k: proof.get(k) for k in ("command", "head_sha", "exit_code", "ran_tests", "finished")}

    if proof.get("exit_code") != 0:
        fail["reason"] = f"suite proof shows exit_code={proof.get('exit_code')} (not a pass)"
        return fail
    if not proof.get("ran_tests"):
        fail["reason"] = "suite proof contains no parsed test count — cannot confirm real tests ran"
        return fail
    if not proof.get("head_sha"):
        fail["reason"] = "suite proof has no HEAD sha — cannot tie the run to any code"
        return fail

    if head_sha and proof["head_sha"] == head_sha:
        return {"ok": True, "valid": True, "reason": None, "proof": fail["proof"]}

    root = get_repo_root(cwd)
    anc = _run(["git", "merge-base", "--is-ancestor", proof["head_sha"], "castle"], root)
    if anc.get("exit_code") == 0:
        return {"ok": True, "valid": True, "reason": None, "proof": fail["proof"]}

    fail["reason"] = (
        f"suite proof HEAD {proof['head_sha'][:12]} is neither the branch tip nor an "
        "ancestor of castle — the suite did not run over the code being promoted"
    )
    return fail
