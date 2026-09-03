"""
Deterministic git/test verification helpers.

All functions are subprocess wrappers returning plain dicts/booleans.
Zero LLM calls. Used by Gatekeeper and Steward for independent verification.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


def _run(cmd: list[str], cwd: Path, timeout: int = 60) -> dict:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
        return {
            "cmd": " ".join(cmd),
            "exit_code": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "ok": proc.returncode == 0,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "cmd": " ".join(cmd),
            "exit_code": None,
            "stdout": "",
            "stderr": f"TIMEOUT after {timeout}s: {e}",
            "ok": False,
        }
    except FileNotFoundError as e:
        return {
            "cmd": " ".join(cmd),
            "exit_code": None,
            "stdout": "",
            "stderr": f"COMMAND NOT FOUND: {e}",
            "ok": False,
        }


def get_repo_root(cwd: Optional[Path] = None) -> Path:
    """Find the top-level directory of the current git repository."""
    base = cwd or Path.cwd()
    res = _run(["git", "rev-parse", "--show-toplevel"], base)
    if res.get("ok") and res.get("stdout"):
        return Path(res["stdout"])
    return Path(__file__).resolve().parent.parent


def list_git_worktrees(cwd: Optional[Path] = None) -> list[dict]:
    """Parse `git worktree list --porcelain` into structured worktree info."""
    root = get_repo_root(cwd)
    res = _run(["git", "worktree", "list", "--porcelain"], root)
    if not res.get("ok") or not res.get("stdout"):
        return []

    worktrees = []
    current: dict = {}
    for line in res["stdout"].splitlines():
        line = line.strip()
        if not line:
            if current and "worktree" in current:
                worktrees.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            if current and "worktree" in current:
                worktrees.append(current)
            current = {"worktree": line[len("worktree "):].strip()}
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            branch_ref = line[len("branch "):].strip()
            current["branch_ref"] = branch_ref
            current["branch"] = branch_ref[len("refs/heads/"):] if branch_ref.startswith("refs/heads/") else branch_ref
        elif line == "detached":
            current["detached"] = True

    if current and "worktree" in current:
        worktrees.append(current)
    return worktrees


def find_worktree_for_branch(branch_or_id: str, cwd: Optional[Path] = None) -> Optional[str]:
    """Find local filesystem worktree path for a given branch name or identifier."""
    clean = branch_or_id.strip()
    if clean.startswith("refs/heads/"):
        clean = clean[len("refs/heads/"):]
    wts = list_git_worktrees(cwd)
    for wt in wts:
        if wt.get("branch") == clean or wt.get("branch_ref") == f"refs/heads/{clean}":
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
    """Check branch, dirty state, and ahead/behind counts for a given worktree."""
    p = Path(worktree_path)
    if not p.exists():
        return {"exists": False, "error": f"path does not exist: {worktree_path}"}

    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], p)
    status = _run(["git", "status", "--porcelain"], p)
    ahead_behind = _run(
        ["git", "rev-list", "--left-right", "--count", "HEAD...@{u}"], p
    )

    dirty = bool(status.get("stdout"))
    ahead = behind = None
    if ahead_behind.get("ok") and ahead_behind.get("stdout"):
        parts = ahead_behind["stdout"].split()
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])

    return {
        "exists": True,
        "branch": branch.get("stdout", "").strip(),
        "dirty": dirty,
        "dirty_files": status.get("stdout", "").splitlines(),
        "ahead": ahead,
        "behind": behind,
    }


def diffstat(worktree_path: str, base_ref: str = "HEAD") -> dict:
    """Return git diff --stat against a given base reference."""
    p = Path(worktree_path)
    return _run(["git", "diff", "--stat", base_ref], p)


def check_merged_status(
    worktree_or_branch: str,
    target_ref: str = "gatehouse",
    base_ref: str = "castle",
    cwd: Optional[Path] = None,
) -> dict:
    """
    Deterministic merge verification and differencing against target_ref and base_ref.
    """
    root = get_repo_root(cwd)
    run_cwd = root
    worktree_path: Optional[Path] = None
    branch: str = ""

    candidate_path = Path(worktree_or_branch)
    if candidate_path.exists() and candidate_path.is_dir():
        worktree_path = candidate_path.resolve()
        run_cwd = worktree_path
        branch_res = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], worktree_path)
        if branch_res.get("ok"):
            branch = branch_res["stdout"].strip()
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
        status_res = _run(["git", "status", "--porcelain"], worktree_path)
        if status_res.get("ok") and status_res.get("stdout"):
            uncommitted_files = [line.strip() for line in status_res["stdout"].splitlines() if line.strip()]
            clean_worktree = len(uncommitted_files) == 0

    branch_ref = branch
    branch_verify = _run(["git", "rev-parse", "--verify", branch_ref], run_cwd)
    if not branch_verify.get("ok"):
        branch_ref_head = f"refs/heads/{branch}"
        if _run(["git", "rev-parse", "--verify", branch_ref_head], run_cwd).get("ok"):
            branch_ref = branch_ref_head
        else:
            return {
                "branch": branch,
                "worktree": str(worktree_path) if worktree_path else None,
                "target_ref": target_ref,
                "base_ref": base_ref,
                "is_merged": False,
                "is_merged_in_target": False,
                "is_merged_in_base": False,
                "is_ancestor": False,
                "is_ancestor_target": False,
                "is_ancestor_base": False,
                "clean_worktree": clean_worktree,
                "uncommitted_files": uncommitted_files,
                "unmerged_commits": [],
                "unmerged_commits_count": 0,
                "unmerged_commits_base": [],
                "unmerged_commits_base_count": 0,
                "diff_stat": "",
                "has_diff": False,
                "cherry_unmerged_count": 0,
                "cherry_merged_count": 0,
                "cherry_unmerged_commits": [],
                "cherry_merged_commits": [],
                "recommendation": f"ERROR: cannot resolve branch ref {branch!r}",
                "ok": False,
                "error": f"Branch ref not found: {branch}",
            }

    target_resolved = target_ref
    if not _run(["git", "rev-parse", "--verify", target_resolved], run_cwd).get("ok"):
        if _run(["git", "rev-parse", "--verify", f"refs/heads/{target_ref}"], run_cwd).get("ok"):
            target_resolved = f"refs/heads/{target_ref}"

    base_resolved = base_ref
    if not _run(["git", "rev-parse", "--verify", base_resolved], run_cwd).get("ok"):
        if _run(["git", "rev-parse", "--verify", f"refs/heads/{base_ref}"], run_cwd).get("ok"):
            base_resolved = f"refs/heads/{base_ref}"

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

    cherry_res = _run(["git", "cherry", "-v", target_resolved, branch_ref], run_cwd)
    cherry_unmerged: list[str] = []
    cherry_merged: list[str] = []
    if cherry_res.get("ok") and cherry_res.get("stdout"):
        for line in cherry_res["stdout"].splitlines():
            if line.startswith("+"):
                cherry_unmerged.append(line[1:].strip())
            elif line.startswith("-"):
                cherry_merged.append(line[1:].strip())

    cherry_unmerged_count = len(cherry_unmerged)
    cherry_merged_count = len(cherry_merged)

    target_has_all_commits = is_ancestor_target or (len(unmerged_commits) == 0) or (cherry_unmerged_count == 0 and len(unmerged_commits) > 0 and not has_diff)
    is_merged_target = target_has_all_commits and not has_diff

    target_in_base = (_run(["git", "merge-base", "--is-ancestor", target_resolved, base_resolved], run_cwd).get("exit_code") == 0)
    is_merged_base = is_ancestor_base or (is_merged_target and target_in_base)

    is_merged = clean_worktree and is_merged_target

    if not clean_worktree:
        recommendation = (
            f"DIRTY_WORKTREE: {len(uncommitted_files)} uncommitted or untracked file(s) present in worktree; "
            f"commit, stash, or clean before teardown"
        )
    elif not is_merged_target:
        if len(unmerged_commits) > 0 and has_diff:
            recommendation = (
                f"UNMERGED: branch has {len(unmerged_commits)} unmerged commit(s) and pending diff against {target_ref}; "
                f"do not teardown"
            )
        elif len(unmerged_commits) > 0:
            recommendation = (
                f"UNMERGED_COMMITS: branch has {len(unmerged_commits)} commit(s) not in {target_ref} history; "
                f"verify cherry-pick/rebase status"
            )
        elif has_diff:
            recommendation = (
                f"DIFF_PRESENT: branch has pending tree diff against {target_ref}; do not teardown"
            )
        else:
            recommendation = f"UNMERGED: branch not merged into {target_ref}; do not teardown"
    elif is_merged_base:
        recommendation = (
            f"SAFE_TO_TEARDOWN: branch fully merged into {target_ref} and promoted to {base_ref}, worktree clean"
        )
    else:
        recommendation = (
            f"MERGED_IN_TARGET: branch merged into {target_ref} (awaiting whole-branch promotion to {base_ref}), worktree clean"
        )

    return {
        "branch": branch,
        "worktree": str(worktree_path) if worktree_path else None,
        "target_ref": target_ref,
        "base_ref": base_ref,
        "is_merged": is_merged,
        "is_merged_in_target": is_merged_target,
        "is_merged_in_base": is_merged_base,
        "is_ancestor": is_ancestor_target,
        "is_ancestor_target": is_ancestor_target,
        "is_ancestor_base": is_ancestor_base,
        "clean_worktree": clean_worktree,
        "uncommitted_files": uncommitted_files,
        "unmerged_commits": unmerged_commits,
        "unmerged_commits_count": len(unmerged_commits),
        "unmerged_commits_base": unmerged_commits_base,
        "unmerged_commits_base_count": len(unmerged_commits_base),
        "diff_stat": diff_stat,
        "has_diff": has_diff,
        "cherry_unmerged_count": cherry_unmerged_count,
        "cherry_merged_count": cherry_merged_count,
        "cherry_unmerged_commits": cherry_unmerged,
        "cherry_merged_commits": cherry_merged,
        "recommendation": recommendation,
        "ok": True,
    }


def run_test_command(worktree_path: str, test_cmd: str, timeout: int = 600) -> dict:
    """Run an arbitrary test or build command inside a worktree directory
    and return exit code with bounded output."""
    p = Path(worktree_path)
    if not p.exists():
        return {"ok": False, "error": f"path does not exist: {worktree_path}"}
    result = _run(["/bin/sh", "-c", test_cmd], p, timeout=timeout)
    for k in ("stdout", "stderr"):
        if len(result.get(k, "")) > 4000:
            result[k] = "...(truncated)...\n" + result[k][-4000:]
    return result


def can_fast_forward(worktree_path: str, base_branch: str = "castle") -> dict:
    """Check whether worktree branch can fast-forward onto base_branch."""
    p = Path(worktree_path)
    fetch = _run(["git", "fetch", "origin", base_branch], p, timeout=60)
    behind_check = _run(
        ["git", "rev-list", "--count", f"HEAD..origin/{base_branch}"], p
    )
    behind = None
    if behind_check.get("ok") and behind_check.get("stdout").isdigit():
        behind = int(behind_check["stdout"])
    return {"fetch_ok": fetch.get("ok"), "behind_base": behind}


def get_branch_diffstat(base: str = "main", head: str = "castle", cwd: Optional[Path] = None) -> dict:
    """Return diffstat between base and head branches (e.g. main..castle) —
    used by `court ship` to show the aggregate promotion diff."""
    p = cwd or Path.cwd()
    return _run(["git", "diff", "--stat", f"{base}..{head}"], p)


def get_branch_log(base: str = "main", head: str = "castle", max_count: int = 50, cwd: Optional[Path] = None) -> dict:
    """Return oneline log of commits between base and head (e.g. git log main..castle --oneline)."""
    p = cwd or Path.cwd()
    return _run(["git", "log", f"{base}..{head}", "--oneline", f"-n{max_count}"], p)


def get_ahead_behind(base: str = "main", head: str = "castle", cwd: Optional[Path] = None) -> dict:
    """Return number of commits head is ahead of / behind base."""
    p = cwd or Path.cwd()
    result = _run(["git", "rev-list", "--left-right", "--count", f"{base}...{head}"], p)
    behind = ahead = None
    if result.get("ok") and result.get("stdout"):
        parts = result["stdout"].split()
        if len(parts) == 2:
            behind, ahead = int(parts[0]), int(parts[1])
    return {"ok": result.get("ok"), "base": base, "head": head, "behind": behind, "ahead": ahead}
