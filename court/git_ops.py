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
