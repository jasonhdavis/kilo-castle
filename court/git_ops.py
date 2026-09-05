"""
Deterministic git/test verification helpers.

All functions are subprocess wrappers returning plain dicts/booleans.
Zero LLM calls. Used by Gatekeeper and Steward for independent verification.
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


def find_worktree_for_quest(quest: Any, cwd: Optional[Path] = None) -> Optional[Path]:
    """Resolve the filesystem path of a Quest's git worktree.

    Checks:
    1. quest.worktree if non-empty and directory exists
    2. Exact branch match in git worktrees
    3. Quest ID match in worktree paths or branch names (handling epics vs quests)
    """
    worktree_attr = getattr(quest, "worktree", "")
    if worktree_attr:
        p = Path(worktree_attr)
        if p.is_dir():
            return p

    worktrees = list_git_worktrees(cwd)
    quest_id = getattr(quest, "id", str(quest))
    branch = getattr(quest, "branch", "")
    kind = getattr(quest, "kind", "quest")
    short_id = quest_id.split("-")[0].lower()  # e.g. "q075"

    # Match by exact branch
    if branch:
        for wt in worktrees:
            if wt.get("branch") == branch or wt.get("branch_ref") == f"refs/heads/{branch}":
                p = Path(wt["worktree"])
                if p.is_dir():
                    return p

    # Match by short id in branch or worktree folder name
    for wt in worktrees:
        wt_path = wt.get("worktree", "")
        wt_branch = wt.get("branch", "")

        # For epics, do not match child quest branches/worktrees
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

        # For regular quests or scouts, match quest id in leaf branch or worktree
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
    """Comprehensive worktree status inspection:
    - dirty/clean state (untracked, modified, staged, deleted)
    - branch name and HEAD sha
    - ahead / behind count against base branch (e.g. castle)
    - diffstat vs base
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

    # Ahead / behind vs base (e.g. castle...HEAD)
    # parts[0] = behind (commits in base not in HEAD)
    # parts[1] = ahead (commits in HEAD not in base)
    ahead_behind_res = _run(["git", "rev-list", "--left-right", "--count", f"{base}...HEAD"], p)
    ahead = behind = None
    if ahead_behind_res.get("ok") and ahead_behind_res.get("stdout"):
        parts = ahead_behind_res["stdout"].split()
        if len(parts) == 2:
            try:
                behind, ahead = int(parts[0]), int(parts[1])
            except ValueError:
                pass

    # Diffstat vs base
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


def _reconcile_history_only_conflict(path: Path) -> bool:
    """For a Quest/Epic ledger file conflicted *only* in its History-table
    rows and/or its `updated_at:` frontmatter line, resolve by taking the
    union of both sides' History rows (deduped, sorted by timestamp) and the
    later `updated_at`.

    This is the routine, expected shape of an "own ledger file" conflict: a
    Serf's own worktree branch and the protected trunk's own bookkeeping both
    append different History rows since the worktree's last rebase — not a
    real content disagreement, just two append-only logs that diverged.
    Returns False (leaving the file untouched) if ANY conflicting block
    contains anything else — e.g. genuinely different Tribute/Goal & Scope
    prose — so real conflicts are always left for a human/Serf to resolve,
    never silently discarded.
    """
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
            merged = list(dict.fromkeys(ours_lines + theirs_lines))  # dedup, stable order
            merged.sort(key=lambda l: _HISTORY_ROW_RE.match(l).group(1))
            return "\n".join(merged)

        return None  # unrecognized content in this block — refuse to guess

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
        return False  # safety net: something didn't fully resolve

    path.write_text(new_text, encoding="utf-8")
    return True


def rebase_worktree_onto_base(
    worktree_path: str | Path,
    base_branch: str = "castle",
    own_quest_id: Optional[str] = None,
    auto_resolve_foreign_ledger: bool = True,
    auto_resolve_own_history: bool = True,
) -> dict:
    """Mechanically converge a worktree onto `base_branch` via `git merge <base> --no-edit`,
    with no LLM agent involved.

    `base_branch` is typically a constantly-moving target in an active repo (every
    Gatekeeper Cog Ship promotion advances it), while the "deferred rebase" convention
    expects a Serf agent to notice drift and run `git merge <base_branch>` themselves
    before REVIEW. At real work-in-progress volume, the round-trip of "notice drift ->
    spawn/prompt an agent -> agent runs the merge" is far slower than the rate the base
    branch advances, so drift only ever grows and the WORKING/REVIEW queue can never
    converge. This function performs the exact same mechanical step deterministically
    and near-instantly (a subprocess call, not an agent turn), so `court levy`/`court
    rebase` can converge an entire backlog of worktrees onto the base branch's current
    tip in one sequential pass without spawning any agents.

    A second failure mode this guards against: if `store.save()`'s auto-commit ever ran
    from the wrong worktree (see `store._commit_allowed_here`), a bulk command could
    poison one Quest's branch with phantom "court: save Q-B" commits for unrelated
    Quests. Since those phantom copies of *other* Quests' `.court/quests/*.md` /
    `.court/epics/*.md` files were never this worktree's real deliverable — the base
    branch is unconditionally authoritative for them — a conflict limited to that class
    of file is safe to auto-resolve by taking the base branch's side
    (`auto_resolve_foreign_ledger=True`, the default). Pass this worktree's own Quest ID
    as `own_quest_id` so its *own* ledger file is exempted from that auto-resolution and
    left as a genuine conflict for a Serf if it disagrees with the base branch.

    Skips (does not touch) a dirty worktree — uncommitted changes must be resolved by a
    Serf first, since merging over them risks stomping in-progress work. On an
    unresolvable merge conflict, aborts cleanly (`git merge --abort`) and reports the
    conflicting paths rather than leaving a half-merged tree for the next command to
    trip over.
    """
    p = Path(worktree_path)
    result: dict = {
        "ok": False,
        "path": str(p),
        "base": base_branch,
        "skipped": None,
        "merged": False,
        "already_up_to_date": False,
        "conflict": False,
        "conflict_files": [],
        "auto_resolved_foreign_ledger_files": [],
        "auto_resolved_history_only_files": [],
        "before_behind": None,
        "after_behind": None,
        "error": None,
    }
    if not p.exists() or not p.is_dir():
        result["error"] = f"worktree path does not exist: {p}"
        return result

    status_res = _run(["git", "status", "--porcelain"], p)
    if status_res.get("ok") and status_res.get("stdout", "").strip():
        result["skipped"] = "dirty"
        result["error"] = "Worktree has uncommitted changes; commit or stash before mechanical rebase."
        return result

    before = get_ahead_behind(base_branch, "HEAD", cwd=p)
    result["before_behind"] = before.get("behind")

    if before.get("behind") in (0, None):
        result["ok"] = True
        result["already_up_to_date"] = True
        result["after_behind"] = before.get("behind")
        return result

    merge_res = _run(["git", "merge", base_branch, "--no-edit"], p, timeout=180)
    if merge_res.get("ok"):
        after = get_ahead_behind(base_branch, "HEAD", cwd=p)
        result["ok"] = True
        result["merged"] = True
        result["after_behind"] = after.get("behind")
        return result

    # Merge failed — almost certainly conflicts. Inspect each conflicting path;
    # any that are *other* Quests'/Epics' ledger files (never this worktree's
    # real deliverable, and the base branch is unconditionally authoritative
    # for them) can be safely auto-resolved by taking the base branch's side.
    # Anything else is a genuine conflict, so abort cleanly rather than leave
    # a half-merged tree.
    conflict_res = _run(["git", "diff", "--name-only", "--diff-filter=U"], p)
    conflict_files = [
        line.strip() for line in conflict_res.get("stdout", "").splitlines() if line.strip()
    ]

    if auto_resolve_foreign_ledger and conflict_files:
        own_prefix = None
        if own_quest_id:
            m = re.match(r"^(Q\d+)", own_quest_id.strip(), re.IGNORECASE)
            if m:
                own_prefix = m.group(1).upper()

        ledger_pattern = re.compile(r"^\.court/(quests|epics)/(Q\d+)-.*\.md$")
        remaining_conflicts = []
        auto_resolved = []
        for f in conflict_files:
            lm = ledger_pattern.match(f)
            is_foreign_ledger = bool(lm) and (own_prefix is None or lm.group(2).upper() != own_prefix)
            resolved = False
            if is_foreign_ledger:
                theirs_res = _run(["git", "checkout", base_branch, "--", f], p)
                if theirs_res.get("ok"):
                    add_res = _run(["git", "add", "--", f], p)
                    resolved = add_res.get("ok", False)
            if resolved:
                auto_resolved.append(f)
            else:
                remaining_conflicts.append(f)

        result["auto_resolved_foreign_ledger_files"] = auto_resolved

        if auto_resolved and not remaining_conflicts:
            # Every conflicting path was a foreign ledger file, now resolved
            # to the base branch's version. Confirm nothing is left unmerged,
            # then complete the merge commit.
            unmerged_check = _run(["git", "diff", "--name-only", "--diff-filter=U"], p)
            if not unmerged_check.get("stdout", "").strip():
                commit_res = _run(["git", "commit", "--no-edit"], p, timeout=60)
                if commit_res.get("ok"):
                    after = get_ahead_behind(base_branch, "HEAD", cwd=p)
                    result["ok"] = True
                    result["merged"] = True
                    result["after_behind"] = after.get("behind")
                    return result
            # Fall through to abort below if the commit didn't take for any reason.
            remaining_conflicts = conflict_files

        conflict_files = remaining_conflicts

    # Second pass: the ONE most common remaining shape is the worktree's own
    # ledger file conflicting with the base branch purely because both sides
    # appended different History-table rows since the worktree's last
    # rebase — not a real content disagreement. Safe to reconcile by union;
    # anything else in the file is left untouched and reported as a genuine
    # conflict.
    if auto_resolve_own_history and conflict_files:
        still_conflicted = []
        history_resolved = []
        for f in conflict_files:
            full_path = p / f
            if _reconcile_history_only_conflict(full_path):
                add_res = _run(["git", "add", "--", f], p)
                if add_res.get("ok"):
                    history_resolved.append(f)
                    continue
            still_conflicted.append(f)

        result["auto_resolved_history_only_files"] = history_resolved

        if history_resolved and not still_conflicted:
            unmerged_check = _run(["git", "diff", "--name-only", "--diff-filter=U"], p)
            if not unmerged_check.get("stdout", "").strip():
                commit_res = _run(["git", "commit", "--no-edit"], p, timeout=60)
                if commit_res.get("ok"):
                    after = get_ahead_behind(base_branch, "HEAD", cwd=p)
                    result["ok"] = True
                    result["merged"] = True
                    result["after_behind"] = after.get("behind")
                    return result
            still_conflicted = conflict_files

        conflict_files = still_conflicted

    result["conflict_files"] = conflict_files
    _run(["git", "merge", "--abort"], p)
    result["conflict"] = True
    result["error"] = merge_res.get("stderr") or merge_res.get("stdout") or "git merge failed"
    return result


def git_commit_paths(
    paths: "list[Path | str] | Path | str",
    commit_msg: str,
    cwd: Optional[Path] = None,
) -> dict:
    """Stage and commit specific file paths atomically without touching other files.
    Returns dict with ok, exit_code, stdout, stderr, cmd.
    Never raises an unhandled exception; callers should check `ok`/`warning`.
    """
    p = cwd or get_repo_root()
    if isinstance(paths, (str, Path)):
        path_list = [Path(paths)]
    else:
        path_list = [Path(x) for x in paths]

    str_paths = []
    p_resolved = p.resolve()
    for item in path_list:
        item_resolved = item.resolve()
        try:
            rel = item_resolved.relative_to(p_resolved)
            str_paths.append(str(rel))
        except ValueError:
            str_paths.append(str(item))

    # Stage only the specific paths
    add_res = _run(["git", "add", "--"] + str_paths, p)
    if not add_res.get("ok"):
        err = add_res.get("stderr") or add_res.get("stdout") or "unknown git error"
        return {
            "ok": False,
            "cmd": add_res.get("cmd"),
            "exit_code": add_res.get("exit_code"),
            "stdout": add_res.get("stdout", ""),
            "stderr": add_res.get("stderr", ""),
            "warning": f"git add failed: {err}",
        }

    # Commit only the specified paths
    commit_cmd = ["git", "commit", "-m", commit_msg, "--"] + str_paths
    commit_res = _run(commit_cmd, p)
    if not commit_res.get("ok"):
        combined_out = f"{commit_res.get('stdout', '')} {commit_res.get('stderr', '')}".lower()
        if "nothing to commit" in combined_out or "no changes added to commit" in combined_out:
            return {
                "ok": True,
                "cmd": commit_res.get("cmd"),
                "exit_code": commit_res.get("exit_code"),
                "stdout": commit_res.get("stdout", ""),
                "stderr": commit_res.get("stderr", ""),
                "no_changes": True,
            }
        err = commit_res.get("stderr") or commit_res.get("stdout") or "unknown git error"
        return {
            "ok": False,
            "cmd": commit_res.get("cmd"),
            "exit_code": commit_res.get("exit_code"),
            "stdout": commit_res.get("stdout", ""),
            "stderr": commit_res.get("stderr", ""),
            "warning": f"git commit failed: {err}",
        }

    return {
        "ok": True,
        "cmd": commit_res.get("cmd"),
        "exit_code": commit_res.get("exit_code"),
        "stdout": commit_res.get("stdout", ""),
        "stderr": commit_res.get("stderr", ""),
    }


def check_proof_of_landing(quest: Any, cwd: Optional[Path | str] = None) -> dict:
    """
    Search the protected trunks — castle, main, and all four the-gatehouse/*
    station branches — for proof that the Quest's work (branch, commit hashes,
    or commit message / concern) actually landed somewhere.
    """
    root = get_repo_root(cwd)
    quest_id = getattr(quest, "id", str(quest))
    branch = getattr(quest, "branch", "")
    concern = getattr(quest, "concern", "")
    short_id = quest_id.split("-")[0].lower()

    # Landing refs are STRICTLY the promotion trunks: castle, main, and the
    # four the-gatehouse/* stations. Never scan arbitrary local branches —
    # including the Quest's own quest/*/scout/* branch — because a commit
    # message on the Quest's own branch mentioning its own ID would count as
    # "proof of landing" and falsely clear a ghost/demote check.
    candidate_refs = [
        "castle",
        "main",
        "the-gatehouse/north",
        "the-gatehouse/south",
        "the-gatehouse/east",
        "the-gatehouse/west",
    ]

    target_refs = []
    for ref in candidate_refs:
        if branch and ref == branch:
            continue  # never accept the quest's own branch as a landing target
        if _run(["git", "rev-parse", "--verify", f"refs/heads/{ref}"], root).get("ok"):
            target_refs.append(ref)

    for ref in target_refs:
        if branch and _run(["git", "rev-parse", "--verify", branch], root).get("ok"):
            ancestor_res = _run(["git", "merge-base", "--is-ancestor", branch, ref], root)
            if ancestor_res.get("exit_code") == 0:
                log_res = _run(["git", "log", "-n1", "--oneline", f"{ref}"], root)
                commit_msg = log_res.get("stdout", "")
                return {
                    "landed": True,
                    "matched_ref": ref,
                    "proof_commit": commit_msg,
                    "reason": f"Branch {branch} is an ancestor of {ref}",
                }

        log_grep = _run(["git", "log", f"{ref}", f"--grep={short_id}", "--oneline", "-n1"], root)
        if log_grep.get("ok") and log_grep.get("stdout").strip():
            return {
                "landed": True,
                "matched_ref": ref,
                "proof_commit": log_grep["stdout"].strip(),
                "reason": f"Found commit matching {short_id} in {ref} log",
            }

        if concern:
            concern_slug = concern.replace("_", "-").lower()
            log_grep_slug = _run(["git", "log", f"{ref}", f"--grep={concern_slug}", "--oneline", "-n1"], root)
            if log_grep_slug.get("ok") and log_grep_slug.get("stdout").strip():
                return {
                    "landed": True,
                    "matched_ref": ref,
                    "proof_commit": log_grep_slug["stdout"].strip(),
                    "reason": f"Found commit matching concern '{concern_slug}' in {ref} log",
                }

    return {
        "landed": False,
        "matched_ref": None,
        "proof_commit": None,
        "reason": "No ancestry or commit log match found in castle, main, or gatehouse station branches.",
    }
