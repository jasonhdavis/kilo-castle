"""
Deterministic git/test verification helpers. No LLM calls here — these are
the facts a Tribute review is checked against. All functions are subprocess
wrappers that return plain dicts/booleans.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Optional

_CONFLICT_BLOCK_RE = re.compile(
    r"<<<<<<< [^\n]*\n(?P<ours>.*?)\n=======\n(?P<theirs>.*?)\n>>>>>>> [^\n]*",
    re.DOTALL,
)
_HISTORY_ROW_RE = re.compile(r"^\|\s*([0-9T:\-Z]+)\s*\|.*\|\s*$")


def _reconcile_history_only_conflict(path: Path) -> bool:
    """For a Quest/Epic ledger file conflicted *only* in its History-table
    rows and/or its `updated_at:` frontmatter line, resolve by taking the
    union of both sides' History rows (deduped, sorted by timestamp) and the
    later `updated_at`."""
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
        if all_rows and all(_HISTORY_ROW_RE.match(l) for l in all_rows):
            merged = list(dict.fromkeys(ours_lines + theirs_lines))
            merged.sort(key=lambda l: _HISTORY_ROW_RE.match(l).group(1))
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


def get_repo_root(cwd: Optional[Path | str] = None) -> Path:
    """Find the top-level directory of the current git repository."""
    base = Path(cwd) if cwd else Path.cwd()
    res = _run(["git", "rev-parse", "--show-toplevel"], base)
    if res.get("ok") and res.get("stdout"):
        return Path(res["stdout"])
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


def list_git_worktrees(cwd: Optional[Path | str] = None) -> list[dict]:
    """Parse `git worktree list --porcelain` into structured records."""
    root = get_repo_root(cwd)
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


def get_worktree_git_status(worktree_path: str | Path, base: str = "castle") -> dict:
    """Comprehensive worktree status inspection."""
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


def run_test_command(worktree_path: str, test_cmd: str, timeout: int = 600) -> dict:
    p = Path(worktree_path)
    if not p.exists():
        return {"ok": False, "error": f"path does not exist: {worktree_path}"}
    result = _run(["/bin/sh", "-c", test_cmd], p, timeout=timeout)
    for k in ("stdout", "stderr"):
        if len(result.get(k, "")) > 4000:
            result[k] = "...(truncated)...\n" + result[k][-4000:]
    return result


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
    if behind_res.get("ok") and behind_res.get("stdout", "").strip() == "0":
        result["ok"] = True
        result["already_current"] = True
        return result

    merge_res = _run(["git", "merge", base_branch, "--no-edit"], p, timeout=120)
    if merge_res.get("ok"):
        result["ok"] = True
        result["merged"] = True
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
