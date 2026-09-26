"""
Migration-graph integrity preflight (Q432, 2026-09-15).

Context: the 2026-09-14 production web deploy (v1383) failed at the release
command because the castle trunk carried two same-parent 0130 migrations in
the `common` app (generated independently by parallel quests Q391/Q401).
That fork is invisible to git (differently-named files merge cleanly) and to
per-quest test suites (each quest only observes its own single leaf) — it
only surfaces when Django builds the whole migration graph at deploy time.

This module hardens the deterministic engine layers so the guard cannot be
skipped:

* `check_migration_graph(worktree)` runs the canonical DB-free graph build
  (``ROLE=web python manage.py makemigrations --check --dry-run``) in the
  given checkout/worktree and parses the verdict, naming the offending app
  and leaf migrations on failure.
* `court ship` calls this (via `cli._ship_preflight`) BEFORE any
  castle->main merge/push/deploy step; a failure aborts with exit non-zero.
* `court runsuite` calls this (via `git_ops.run_unified_suite`) as a stage
  of the unified suite and stamps ``migration_graph_ok`` into the
  engine-stamped suite proof JSON; a failure makes the suite exit non-zero.

DB-free note: `makemigrations` builds the graph from on-disk migration files
plus the in-memory model state (``MigrationLoader(None)``). Django's only
database touch on this path is a non-fatal applied-history consistency read
that degrades to a RuntimeWarning when no database is reachable, so the
check works with zero database dependency and typically completes in a few
seconds. Engine code here is stdlib-only (subprocess + re).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

# The canonical, human-runnable form of the check (charter-specified). Used
# verbatim (via bash) whenever the target checkout has its own virtualenv.
MIGRATION_GRAPH_SHELL_COMMAND = (
    "source venv/bin/activate && ROLE=web python manage.py makemigrations --check --dry-run"
)

# Reason: quest/gatehouse worktrees are fresh checkouts that normally carry a
# copied `.env` but NOT their own venv (gitignored, lives in the parent
# checkout). For those, run the same command argv-style with the parent
# repo's venv interpreter — the only effect `source venv/bin/activate` has on
# this path is putting that same interpreter first on PATH.
MIGRATION_GRAPH_ARGV_COMMAND = ["manage.py", "makemigrations", "--check", "--dry-run"]

DEFAULT_TIMEOUT = 300

# Django 5.x CommandError emitted when two+ migrations share a graph leaf:
#   Conflicting migrations detected; multiple leaf nodes in the migration
#   graph: (0130_a, 0130_b in common).
# The parenthesized body lists comma-separated leaf names per app, joined by
# "; " across apps (see django.core.management.commands.makemigrations).
_CONFLICT_RE = re.compile(
    r"Conflicting migrations detected; multiple leaf nodes in the migration "
    r"graph: \((?P<body>[^)]*)\)"
)

# `--check` failing on un-migrated model changes prints one block per app:
#   Migrations for 'inventory':
#     apps/inventory/migrations/0037_...py
_MISSING_APP_RE = re.compile(r"^Migrations for '([^']+)':", re.MULTILINE)

# Settings/environment failures (missing .env in a fresh worktree, missing
# required env vars, import errors) are NOT graph verdicts — but at a deploy
# gate an unverifiable graph must fail closed, with a distinct message.
_ENV_FAILURE_MARKERS = (
    "environment variable is required",
    "ImproperlyConfigured",
    "ModuleNotFoundError",
    "ImportError",
    "ValueError:",
)


def _find_parent_venv_python(worktree: Path) -> Optional[str]:
    """Locate the parent (main) checkout's venv python for worktrees that do
    not have their own venv. `git rev-parse --git-common-dir` resolves to the
    primary .git directory even from a linked worktree."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    git_common_dir = Path(proc.stdout.strip())
    candidate = git_common_dir.parent / "venv" / "bin" / "python"
    if candidate.is_file():
        return str(candidate)
    return None


def build_migration_graph_command(worktree: Path) -> tuple[list[str], dict]:
    """Return (argv, env) for the graph check in `worktree`.

    * Checkout with its own venv -> the exact charter-specified shell form
      (``source venv/bin/activate && ROLE=web python manage.py makemigrations
      --check --dry-run``) run through bash.
    * Checkout without a venv (quest/gatehouse worktrees) -> argv form using
      the parent checkout's venv python, falling back to the interpreter
      running the engine itself.
    """
    if (worktree / "venv" / "bin" / "activate").is_file():
        return (["bash", "-c", MIGRATION_GRAPH_SHELL_COMMAND], dict(os.environ))
    python = _find_parent_venv_python(worktree) or sys.executable
    env = dict(os.environ)
    env["ROLE"] = "web"
    return ([python] + MIGRATION_GRAPH_ARGV_COMMAND, env)


def _parse_offenders(exit_code: int, output: str) -> tuple[list[dict], str]:
    """Classify a failed graph check into offenders + failure mode.

    Returns (offenders, mode) where offenders is a list of
    {"app": str, "leaves": [str, ...]} and mode is one of
    "multiple_leaf_nodes" | "missing_migrations" | "environment" | "unknown".
    """
    conflicts = []
    for match in _CONFLICT_RE.finditer(output):
        for chunk in match.group("body").split(";"):
            chunk = chunk.strip()
            if " in " not in chunk:
                continue
            names, app = chunk.rsplit(" in ", 1)
            leaves = [n.strip() for n in names.split(",") if n.strip()]
            if leaves:
                conflicts.append({"app": app.strip(), "leaves": leaves})
    if conflicts:
        return conflicts, "multiple_leaf_nodes"

    missing_apps = _MISSING_APP_RE.findall(output)
    if missing_apps:
        return [{"app": app, "leaves": []} for app in missing_apps], "missing_migrations"

    if any(marker in output for marker in _ENV_FAILURE_MARKERS):
        return [], "environment"

    return [], "unknown"


def _describe_offenders(offenders: list[dict], mode: str) -> str:
    if mode == "multiple_leaf_nodes":
        parts = [
            "app '%s': leaf migrations %s" % (o["app"], ", ".join(o["leaves"]))
            for o in offenders
        ]
        return (
            "migration graph has multiple leaf nodes — "
            + "; ".join(parts)
            + " (reconcile with 'python manage.py makemigrations --merge')"
        )
    if mode == "missing_migrations":
        apps = ", ".join("'%s'" % o["app"] for o in offenders)
        return (
            "model changes not reflected in migrations for app(s) "
            + apps
            + " (generate the pending migrations before deploying)"
        )
    if mode == "environment":
        return (
            "graph check could not run: settings/environment failure (not a "
            "graph verdict) — the migration graph could not be verified"
        )
    return "graph check failed for an unrecognized reason"


def check_migration_graph(worktree: str | Path, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run the DB-free migration-graph build in `worktree` and return a plain
    verdict dict (engine stdlib-only):

    {
        "ok": bool,                 # True only when the graph builds clean
        "exit_code": int | None,    # makemigrations process exit code
        "command": str,             # exact command run
        "mode": str,                # "clean" | "multiple_leaf_nodes" | ...
        "offenders": [{"app": str, "leaves": [str, ...]}, ...],
        "reason": str | None,       # human-readable failure message
        "output_tail": str,         # last 4000 chars of combined output
    }

    Any non-zero exit is a failure (fail closed): a fork, pending model
    changes, or an environment that cannot even build the graph all block
    deployment — at a deploy gate "could not verify" must not read as pass.
    """
    p = Path(worktree).resolve()
    result: dict = {
        "ok": False,
        "exit_code": None,
        "command": MIGRATION_GRAPH_SHELL_COMMAND,
        "mode": "unknown",
        "offenders": [],
        "reason": None,
        "output_tail": "",
    }
    if not p.is_dir() or not (p / "manage.py").is_file():
        result["reason"] = f"no manage.py in checkout: {p} — graph check cannot run"
        return result

    argv, env = build_migration_graph_command(p)
    result["command"] = " ".join(argv) if argv[0] != "bash" else MIGRATION_GRAPH_SHELL_COMMAND
    try:
        proc = subprocess.run(
            argv,
            cwd=str(p),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        result["mode"] = "timeout"
        result["reason"] = f"graph check exceeded engine timeout ({timeout}s)"
        return result
    except OSError as e:
        result["mode"] = "environment"
        result["reason"] = f"graph check could not be executed: {e}"
        return result

    output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    result["exit_code"] = proc.returncode
    result["output_tail"] = output[-4000:]

    if proc.returncode == 0:
        result["ok"] = True
        result["mode"] = "clean"
        return result

    offenders, mode = _parse_offenders(proc.returncode, output)
    result["offenders"] = offenders
    result["mode"] = mode
    result["reason"] = _describe_offenders(offenders, mode)
    return result
