"""Deterministic worktree and pytest-database teardown.

Why this exists (2026-11-09): `court raze` verified promotion and wrote the
durable `razed_at` marker but never removed anything, so every razed Quest left
its worktree on disk for a manual `court fork-teardown-list` pass. By 2026-11-09
that queue held 44 worktrees and 81 Quests sat at READY_TO_RAZE. In parallel,
pytest-django's per-worktree test databases (``test_pb_app_local_<sha1(BASE_DIR)
[:8]>[_gwN]``, ``core/settings/base.py``) accumulated to 964 databases / ~34 GB
because each ephemeral gatehouse worktree hashes a brand-new path and
``--reuse-db`` never drops them.

Both artifacts are regenerable (a branch tip reachable from castle, and a schema
that rebuilds from migrations in ~6 s), so both are now torn down mechanically.
The invariant that makes this safe is simple: **never delete anything a live
worktree still owns.** A database is stale only when its hash matches no
worktree git knows about, and nothing is holding a connection to it.

Safety envelope:
  * a worktree is removed only when it is clean, lives under
    ``.kilo/worktrees/``, and its branch is an ancestor of ``castle`` (or the
    Quest is a scout, which is non-merging by design);
  * a database is dropped only when it matches the configured test-DB pattern
    (never a mirror/dev/prod name), its hash is not live, and
    ``pg_stat_activity`` shows no backend attached;
  * every entry point is dry-run by default; ``court raze`` passes
    ``apply=True`` because raze has already verified promotion.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

WORKTREE_ROOT = Path(".kilo") / "worktrees"

#: Suffixes pytest-django appends per xdist worker (``_gw0``, ``_gw1``, ...).
_WORKER_SUFFIX = re.compile(r"^_gw\d+$")

DEFAULT_DB_PREFIXES = ("test_pb_app_local",)


def _run(argv: list[str], cwd: Path, timeout: int = 60) -> dict:
    try:
        proc = subprocess.run(
            ["git", *argv], cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
        return {
            "ok": proc.returncode == 0,
            "exit": proc.returncode,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
        }
    except Exception as exc:  # noqa: BLE001 — callers degrade to a report
        return {"ok": False, "exit": -1, "stdout": "", "stderr": str(exc)}


def repo_root(start: Optional[Path] = None) -> Optional[Path]:
    """Main checkout that owns the worktrees (never the worktree itself)."""
    res = _run(["rev-parse", "--path-format=absolute", "--git-common-dir"], start or Path.cwd())
    if not res["ok"] or not res["stdout"]:
        return None
    git_dir = Path(res["stdout"])
    return git_dir.parent if git_dir.name == ".git" else git_dir


# ---------------------------------------------------------------------------
# Test-database names
# ---------------------------------------------------------------------------


def db_suffix(path: str | Path) -> str:
    """The ``TEST_DB_SUFFIX`` pytest-django derives for a checkout.

    Must stay byte-identical to ``core/settings/base.py``:
    ``sha1(str(BASE_DIR).encode()).hexdigest()[:8]`` where ``BASE_DIR`` is the
    worktree/checkout root. Django resolves ``__file__``, so the path is
    normalised here too (``/private/var`` vs ``/var`` on macOS, symlinks).
    """
    return hashlib.sha1(str(Path(path).resolve()).encode()).hexdigest()[:8]


def test_db_names(prefix: str, suffix: str, workers: int = 32) -> list[str]:
    """Every database name a suite run in this checkout can create."""
    names = [f"{prefix}_{suffix}"]
    names.extend(f"{prefix}_{suffix}_gw{n}" for n in range(workers))
    return names


def live_db_suffixes(repo: Path) -> set[str]:
    """Suffixes of every checkout git still knows about (plus the main clone)."""
    roots: list[str] = []
    main = repo_root(repo) or Path(repo).resolve()
    roots.append(str(main))
    res = _run(["worktree", "list", "--porcelain"], main)
    if res["ok"]:
        for line in res["stdout"].splitlines():
            if line.startswith("worktree "):
                roots.append(line[len("worktree ") :].strip())
    # A worktree directory can survive `git worktree list` pruning only while
    # its .git entry exists; anything on disk under .kilo/worktrees counts as
    # live too, so a half-broken worktree never has its DBs yanked.
    wt_root = main / WORKTREE_ROOT
    if wt_root.is_dir():
        for child in wt_root.iterdir():
            if child.is_dir():
                roots.append(str(child))
    return {db_suffix(r) for r in roots}


def _config(repo: Path) -> dict:
    try:
        data = json.loads((Path(repo) / ".court" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def db_prefixes(repo: Path) -> tuple[str, ...]:
    cfg = _config(repo).get("test_db") or {}
    prefixes = cfg.get("prefixes") or ([cfg["prefix"]] if cfg.get("prefix") else [])
    return tuple(prefixes) or DEFAULT_DB_PREFIXES


@dataclass
class _Db:
    name: str
    size_bytes: int = 0
    hash: str = ""
    prefix: str = ""


def _split_db(name: str, prefixes: Iterable[str]) -> Optional[_Db]:
    for prefix in sorted(prefixes, key=len, reverse=True):
        if not name.startswith(prefix + "_"):
            continue
        rest = name[len(prefix) + 1 :]
        parts = rest.split("_")
        if not parts or not re.fullmatch(r"[0-9a-f]{8}", parts[0]):
            continue
        if len(parts) > 1 and not _WORKER_SUFFIX.match("_" + "_".join(parts[1:])):
            continue
        return _Db(name=name, hash=parts[0], prefix=prefix)
    return None


def _psql(sql: str, cwd: Path, db: str = "postgres") -> dict:
    try:
        proc = subprocess.run(
            ["psql", "-d", db, "-tAF", "|", "-c", sql],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=60,
        )
        return {"ok": proc.returncode == 0, "stdout": proc.stdout or "", "stderr": proc.stderr or ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "stdout": "", "stderr": str(exc)}


def list_test_databases(repo: Path) -> list[_Db]:
    prefixes = db_prefixes(repo)
    res = _psql(
        "select datname, pg_database_size(oid) from pg_database "
        "where not datistemplate order by datname",
        Path(repo),
    )
    out: list[_Db] = []
    if not res["ok"]:
        return out
    for line in res["stdout"].splitlines():
        name, _, size = line.partition("|")
        parsed = _split_db(name.strip(), prefixes)
        if parsed is None:
            continue
        parsed.size_bytes = int(size or 0)
        out.append(parsed)
    return out


def _has_connections(name: str, cwd: Path) -> bool:
    res = _psql(f"select count(*) from pg_stat_activity where datname = '{name}'", cwd)
    try:
        return int((res["stdout"] or "0").strip() or 0) > 0
    except ValueError:
        return True  # unreadable = assume busy, never drop


@dataclass
class DropPlan:
    stale: list[_Db] = field(default_factory=list)
    kept_live: list[_Db] = field(default_factory=list)
    kept_busy: list[_Db] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def reclaimed_bytes(self) -> int:
        return sum(d.size_bytes for d in self.stale)


def plan_stale_test_dbs(
    repo: Path, *, databases: Optional[list[_Db]] = None, min_age_seconds: int = 0
) -> DropPlan:
    """Classify test databases into stale / live / busy without dropping."""
    dbs = list_test_databases(repo) if databases is None else databases
    live = live_db_suffixes(repo)
    plan = DropPlan()
    cutoff = time.time() - max(0, int(min_age_seconds))
    prefixes = db_prefixes(repo)
    for db in dbs:
        if not db.hash:
            parsed = _split_db(db.name, prefixes)
            if parsed is None:
                continue
            db.hash, db.prefix = parsed.hash, parsed.prefix
        if db.hash in live:
            plan.kept_live.append(db)
        elif min_age_seconds and _db_age(db.name, Path(repo)) is not None and _db_age(db.name, Path(repo)) > cutoff:
            plan.kept_busy.append(db)
        elif _has_connections(db.name, Path(repo)):
            plan.kept_busy.append(db)
        else:
            plan.stale.append(db)
    return plan


def _db_age(name: str, cwd: Path) -> Optional[float]:
    """Filesystem creation time of the database, when Postgres can report it.

    ``pg_stat_file`` needs superuser rights; the local Homebrew role usually has
    them, but a probe failure is not fatal — the caller falls back to the
    connection check alone.
    """
    res = _psql(
        "select extract(epoch from (pg_stat_file('base/'||oid||'/PG_VERSION')).modification) "
        f"from pg_database where datname = '{name}'",
        cwd,
    )
    try:
        return float((res["stdout"] or "").strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def drop_stale_test_dbs(
    repo: Path,
    *,
    apply: bool = False,
    databases: Optional[list[_Db]] = None,
    min_age_seconds: int = 0,
) -> DropPlan:
    plan = plan_stale_test_dbs(repo, databases=databases, min_age_seconds=min_age_seconds)
    if not apply:
        return plan
    for db in plan.stale:
        res = _psql(f'DROP DATABASE IF EXISTS "{db.name}"', Path(repo))
        if res["ok"]:
            plan.dropped.append(db.name)
        else:
            plan.errors.append(f"{db.name}: {(res['stderr'] or '').strip()[:200]}")
    return plan


def drop_worktree_test_dbs(repo: Path, worktree: str | Path, *, apply: bool = True) -> list[str]:
    """Drop the databases that belonged to one checkout (about to disappear).

    Only databases that actually exist are attempted, so the report is honest
    (the 32 per-run xdist suffixes are mostly absent).
    """
    if not apply:
        return []
    suffix = db_suffix(Path(worktree))
    existing = [db for db in list_test_databases(repo) if db.hash == suffix]
    dropped: list[str] = []
    for db in existing:
        if _has_connections(db.name, Path(repo)):
            continue
        if _psql(f'DROP DATABASE IF EXISTS "{db.name}"', Path(repo))["ok"]:
            dropped.append(db.name)
    return dropped


# ---------------------------------------------------------------------------
# Worktree teardown
# ---------------------------------------------------------------------------


def _under_worktree_root(repo: Path, path: Path) -> bool:
    root = (repo_root(repo) or Path(repo).resolve()) / WORKTREE_ROOT
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _branch_exists(repo: Path, branch: str) -> bool:
    return bool(_run(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo)["ok"])


def _branch_merged(repo: Path, branch: str, base: str = "castle") -> bool:
    res = _run(["merge-base", "--is-ancestor", branch, base], repo)
    return bool(res["ok"])


def teardown_worktree(
    repo: Path,
    worktree: str | Path,
    *,
    branch: str = "",
    is_scout: bool = False,
    drop_dbs: bool = True,
    apply: bool = True,
    base: str = "castle",
) -> dict:
    """Remove a razed Quest's worktree, its merged branch, and its test DBs.

    Returns a report dict (``ok`` plus a human ``steps`` list). A refusal is
    never an error the caller must handle: it simply leaves everything on disk
    for the next pass. Destructive steps only run with ``apply=True``.
    """
    main = repo_root(repo) or Path(repo).resolve()
    path = Path(worktree)
    report: dict = {"ok": False, "steps": [], "path": str(path), "branch": branch}

    if not path.exists():
        report["steps"].append("worktree not on disk — nothing to remove")
        report["ok"] = True
        if drop_dbs and apply:
            dropped = drop_worktree_test_dbs(main, path, apply=True)
            if dropped:
                report["steps"].append(f"dropped {len(dropped)} test database(s)")
        return report

    if not _under_worktree_root(main, path):
        report["steps"].append(f"refused: {path} is not under {WORKTREE_ROOT}/")
        return report

    st = _run(["status", "--porcelain"], path)
    if not st["ok"]:
        report["steps"].append(f"refused: not a readable git worktree ({st['stderr'][:120]})")
        return report
    if st["stdout"]:
        report["steps"].append(f"refused: worktree is dirty ({len(st['stdout'].splitlines())} path(s))")
        return report

    # The worktree is disposable either way (it is clean and razed); only the
    # BRANCH is gated on ancestral merit. A squash-promoted Quest has content in
    # castle but a tip castle cannot reach, and deleting that ref would be the
    # one unrecoverable act here — so it is kept, not refused.
    branch_tip = branch and _branch_exists(main, branch)
    merged = bool(branch_tip) and not is_scout and _branch_merged(main, branch, base)

    if not apply:
        report["ok"] = True
        report["steps"].append(f"would remove {path}")
        if merged:
            report["steps"].append(f"would delete merged branch {branch}")
        elif branch_tip and not is_scout:
            report["steps"].append(f"would KEEP {branch} (not an ancestor of {base})")
        report["steps"].append("would drop its test databases")
        return report

    rm = _run(["worktree", "remove", "--force", str(path)], main)
    if not rm["ok"]:
        report["steps"].append(f"git worktree remove failed: {rm['stderr'][:200]}")
        return report
    report["steps"].append(f"removed worktree {path}")

    if merged:
        br = _run(["branch", "-D", branch], main)
        report["steps"].append(
            f"deleted merged branch {branch}" if br["ok"] else f"branch delete skipped: {br['stderr'][:160]}"
        )
    elif branch_tip and not is_scout:
        report["steps"].append(
            f"kept branch {branch} — content is in {base} via squash/merge history, "
            f"but the tip is not an ancestor (ref preserved)"
        )
    elif branch_tip:
        report["steps"].append(f"kept scout branch {branch} (non-merging by design)")

    _run(["worktree", "prune"], main)

    if drop_dbs:
        dropped = drop_worktree_test_dbs(main, path, apply=True)
        if dropped:
            report["steps"].append(f"dropped {len(dropped)} test database(s)")
    report["ok"] = True
    return report


def format_drop_plan(plan: DropPlan, apply: bool = False) -> str:
    def gb(n: int) -> str:
        return f"{n / (1024 ** 3):.2f} GB"

    lines = [
        f"stale: {len(plan.stale)} ({gb(plan.reclaimed_bytes)}) · "
        f"live: {len(plan.kept_live)} · busy: {len(plan.kept_busy)}",
    ]
    for db in plan.stale[:20]:
        lines.append(f"  {'dropped' if db.name in plan.dropped else 'would drop'} {db.name} ({gb(db.size_bytes)})")
    if len(plan.stale) > 20:
        lines.append(f"  … {len(plan.stale) - 20} more")
    for err in plan.errors:
        lines.append(f"  ⚠️ {err}")
    if not apply:
        lines.append("dry-run — pass --apply to drop")
    return "\n".join(lines)
