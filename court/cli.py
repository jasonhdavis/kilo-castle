#!/usr/bin/env python3
"""
Court CLI — deterministic Quest/Epic ledger operations.

Usage examples:
    python3 -m court.cli new --app marketing --concern template-draft-held \\
        --title "MessageTemplate id 34 stuck in draft, holding 99 OutboundMessage rows" \\
        --section "Bug fix"

    python3 -m court.cli status
    python3 -m court.cli show Q001
    python3 -m court.cli advance Q001 WORKING --note "Serf dispatched"
    python3 -m court.cli set-field Q001 branch fix/mkt-template-draft-held
    python3 -m court.cli set-section Q001 "Expected Tribute" --file /tmp/tribute.md
     python3 -m court.cli verify Q001 --test-cmd "python manage.py test apps.marketing"
     python3 -m court.cli worktree-doc Q079 --diff
     python3 -m court.cli teardown-list
    python3 -m court.cli archive Q001
    python3 -m court.cli rollup --section ballad --epic Q012
    python3 -m court.cli edict "Priority on demand forecasting stabilization"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import store
from .models import Quest, STATUSES, SECTIONS, KINDS, now_iso, validate_branch_name, status_label
from . import git_ops
from . import ward
from . import branch_ops
from . import config
from . import migration_guard
from . import migration_graph
from . import browser as studio_browser
from . import studio_close

# The Ward's durable workspace: patrol ledger + Warden Report queue.
WARD_DIR = Path(__file__).resolve().parent.parent / "ward"
WARDENS_LOG_PATH = WARD_DIR / "WARDENS_LOG.md"
WARD_REPORTS_DIR = WARD_DIR / "reports"

# Q455: role models are resolved through the config loader against the single
# manifest (.court/config.json) — no model-ID constants live in engine code.
# Role list: config.KNOWN_ROLE_MODELS. Provider fallbacks: config.get_provider.
SERF_DISPATCH_TEMPLATE = ".court/templates/serf_dispatch_prompt.md"
ARTIST_DISPATCH_TEMPLATE = ".court/templates/court_artist_prompt.md"
ATELIER_DISPATCH_TEMPLATE = ".court/templates/court_artist_convoy_prompt.md"
STUDIO_DISPATCH_TEMPLATE = ".court/templates/court_artist_studio_prompt.md"
REPO_ROOT = git_ops.get_repo_root()
MANAGE_SERVERS_PATH = REPO_ROOT / ".kilo" / "manage_servers.sh"

# Pipeline order for `court charter`'s idempotent advance-to-PLANNED (Q183).
# Side-states (HELD/PUNISHED) are deliberately excluded: chartering never
# pulls a Quest out of a side-state — that is the pillory/successor flow's
# job, not a charter re-run's.
_CHARTER_PIPELINE_ORDER = (
    "OPEN", "PLANNED", "DISPATCHED", "WORKING", "TRIBUTE_READY", "GATE", "READY_TO_RAZE", "DONE",
)


def find_kilo_binary() -> Optional[Path]:
    """Find the kilo CLI binary on the system.

    Checks:
    1. KILO_BIN environment variable
    2. shutil.which("kilo")
    3. VS Code extensions directory: ~/.vscode/extensions/kilocode.kilo-code-*/bin/kilo
    4. Common local paths: ~/.local/bin/kilo, /usr/local/bin/kilo
    """
    env_bin = os.environ.get("KILO_BIN")
    if env_bin and Path(env_bin).is_file() and os.access(env_bin, os.X_OK):
        return Path(env_bin)

    which_bin = shutil.which("kilo")
    if which_bin:
        return Path(which_bin)

    # Check VS Code extension installs
    vscode_ext = Path.home() / ".vscode" / "extensions"
    if vscode_ext.is_dir():
        matches = sorted(vscode_ext.glob("kilocode.kilo-code-*/bin/kilo"), reverse=True)
        for m in matches:
            if m.is_file() and os.access(m, os.X_OK):
                return m

    for fallback in [Path.home() / ".local" / "bin" / "kilo", Path("/usr/local/bin/kilo")]:
        if fallback.is_file() and os.access(fallback, os.X_OK):
            return fallback

    return None


def _court_package_root(worktree_path: Optional[Path] = None) -> Optional[Path]:
    """Locate the checkout that owns the importable `court` package.

    The engine package is git-excluded from target repositories (`court init`
    vendors only .court/, .kilo/ assets), so a worktree spawned inside one of
    those repos has NO court/ of its own — roles there must import from the
    parent checkout (the exact reach-around whose permission prompt kills
    headless runs). Resolution order:
      1. the worktree itself (castle's own worktrees track court/)
      2. the main checkout (git common-dir parent)
      3. the first ancestor directory with court/__init__.py
      4. the site-installed court package
    """
    candidates: list[Path] = []
    if worktree_path is not None:
        wt = Path(worktree_path)
        candidates.append(wt)
        try:
            res = git_ops._run(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], wt
            )
            common = (res.get("stdout") or "").strip()
            if common:
                candidates.append(Path(common).parent)
        except Exception:
            pass
        cur = wt.resolve()
        for _ in range(6):
            parent = cur.parent
            if parent == cur:
                break
            cur = parent
            candidates.append(cur)
    try:
        import importlib.util

        spec = importlib.util.find_spec("court")
        if spec and spec.origin:
            candidates.append(Path(spec.origin).resolve().parent.parent)
    except Exception:
        pass
    for cand in candidates:
        if cand and (cand / "court" / "__init__.py").is_file():
            return cand
    return None


def _engine_spawn_env(worktree_path: Optional[Path] = None) -> dict:
    """Environment for engine-spawned worker processes: PYTHONPATH is
    prepended with the court-owning checkout so `python3 -m court.cli` (and
    `court runsuite`) resolve inside ANY worktree — no more reaching into the
    parent checkout via an extra shell permission."""
    env = os.environ.copy()
    pkg_root = _court_package_root(worktree_path)
    if pkg_root is None:
        return env
    parts = [str(pkg_root)]
    existing = env.get("PYTHONPATH", "")
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


COURT_SHIM_HEADER = """#!/bin/sh
# court engine launcher shim: resolves the engine from the checkout that owns
# the `court` package, so roles inside git-excluded worktrees can run
# `.kilo/court <subcommand>` (e.g. `.kilo/court runsuite --cogship X`) without
# an extra permission prompt to reach the parent checkout.
"""


def write_court_shim(worktree_path: Path) -> Optional[Path]:
    """Write the `.kilo/court` launcher shim into a worktree (idempotent)."""
    pkg_root = _court_package_root(worktree_path)
    if pkg_root is None:
        return None
    kilo_dir = worktree_path / ".kilo"
    try:
        kilo_dir.mkdir(parents=True, exist_ok=True)
        shim = kilo_dir / "court"
        shim.write_text(
            COURT_SHIM_HEADER
            + f'PYTHONPATH="{pkg_root}$([ -n "$PYTHONPATH" ] && echo ":$PYTHONPATH")" '
            + 'exec python3 -m court.cli "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(shim.stat().st_mode | 0o111)
        return shim
    except OSError:
        return None


def build_serf_task_prompt(quest: Quest) -> str:
    """Generate the clean, pure task-only prompt for a Serf.

    Zero persona boilerplate; zero prompt corruption. The Serf persona and
    behavioral constraints live in Kilo's system prompt (.kilo/agent/serf.md or kilo.json).
    """
    branch = quest.branch or quest.tree_branch
    lines = [
        f"# Quest {quest.id}: {quest.title}",
        f"Branch: {branch}",
        f"Section: {quest.section or '-'}",
        "",
        "## The Kingdom Requires",
        quest.body_sections.get("The Kingdom Requires") or quest.body_sections.get("Goal & Scope") or "",
        "",
        "## Expected Tribute",
        quest.body_sections.get("Expected Tribute") or "",
    ]

    scout_of = getattr(quest, "scout_of", "") or ""
    if scout_of:
        lines.extend([
            "",
            "## Scout Predecessor",
            f"This Quest implements the findings of Scout spike **{scout_of}**. Before writing new code:",
            f"1. Read that Scout's report: `python3 -m court.cli show {scout_of}` (5-part Survey/Map/Dangers/Tribute/Plot report in its Tribute Rendered section).",
            f"2. Its worktree may still be live — check `python3 -m court.cli show {scout_of}` for its `worktree` path, or list worktrees if not yet torn down.",
            f"3. Copy over anything reusable from that worktree into THIS worktree: mock scripts, ad-hoc probe commands, scratch fixtures/test data, and any partial implementation the Scout validated.",
            f"4. That source Scout will auto-transition to `READY_TO_RAZE` now that you are actively building the production version.",
        ])

    return "\n".join(lines).strip() + "\n"


def setup_worktree_agent_config(worktree_path: Path, agent: str = "serf") -> None:
    """Ensure a worktree directory has worktree-scoped configuration
    defaulting to the specified agent (default: 'serf')."""
    kilo_dir = worktree_path / ".kilo"
    kilo_dir.mkdir(parents=True, exist_ok=True)
    # `.kilo/court` launcher shim: in target repos the court package is
    # git-excluded, so worktree roles need a stable way to invoke the engine
    # without a parent-checkout permission prompt (Q-rec: court importable in
    # worktrees). Written on every standup; idempotent.
    write_court_shim(worktree_path)
    cfg_file = kilo_dir / "kilo.json"
    cfg = {"$schema": "https://app.kilo.ai/config.json", "default_agent": agent}
    if cfg_file.is_file():
        try:
            existing = json.loads(cfg_file.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                existing["default_agent"] = agent
                cfg = existing
        except Exception:
            pass
    cfg_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def ensure_worktree_browser_mcp(worktree_path: Path, port: int) -> bool:
    """Merge the chrome-devtools MCP server into the worktree's untracked
    ``.kilo/kilo.json`` (the same file setup_worktree_agent_config maintains),
    preserving every existing key. Returns True on success.

    Its tools attach to the shared studio browser over CDP automatically.
    """
    try:
        kilo_dir = worktree_path / ".kilo"
        kilo_dir.mkdir(parents=True, exist_ok=True)
        cfg_file = kilo_dir / "kilo.json"
        cfg: dict[str, Any] = {}
        if cfg_file.is_file():
            try:
                existing = json.loads(cfg_file.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    cfg = existing
            except Exception:
                cfg = {}
        mcp = cfg.get("mcp") if isinstance(cfg.get("mcp"), dict) else {}
        mcp["chrome-devtools"] = {
            "type": "local",
            "command": [
                "npx", "-y", "chrome-devtools-mcp@latest",
                "--browserUrl", f"http://127.0.0.1:{int(port)}",
            ],
            "enabled": True,
        }
        cfg["mcp"] = mcp
        cfg_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


# Q455: canonical_model_id moved to engine.config — alias resolution now reads
# the manifest's model_aliases block instead of an engine-side hardcoded table.
canonical_model_id = config.canonical_model_id


SESSION_FRESH_MS = 30 * 60 * 1000  # 30 min: a session updated within this window is presumed live


def latest_session_activity_ms(worktree_path: Path) -> Optional[int]:
    """Return the max session time_updated (epoch ms) recorded in kilo.db for a
    worktree, or None when the db is missing/unreadable or the worktree has no
    sessions."""
    db_path = Path.home() / ".local" / "share" / "kilo" / "kilo.db"
    wt_str = str(worktree_path.resolve())
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=1.0)
        cur = conn.cursor()
        cur.execute(
            "SELECT MAX(time_updated) FROM session WHERE directory = ?",
            (wt_str,),
        )
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return int(row[0])
    except Exception:
        pass
    return None


def session_is_fresh(worktree_path: Path, max_age_ms: int = SESSION_FRESH_MS) -> bool:
    """True when the worktree has a kilo session updated within max_age_ms."""
    act = latest_session_activity_ms(worktree_path)
    if act is None:
        return False
    return (time.time() * 1000 - act) <= max_age_ms


def gatehouse_convoy_is_live(cogship_id: str, repo_root: Optional[Path] = None) -> tuple[bool, str]:
    """Mechanical version of the 'grep cogship_id before standing up a
    Gatekeeper' ritual (2026-09-09 lesson): a convoy is live when its ephemeral
    gatehouse worktree exists AND has a freshly-updated kilo session (an active
    Gatekeeper), or when the convoy branch exists ahead of castle."""
    norm = store.normalize_cogship_id(cogship_id)
    if not norm:
        return False, "unnormalized cogship id"
    root = repo_root or git_ops.get_repo_root()
    wt = root / ".kilo" / "worktrees" / f"the-gatehouse-{norm}"
    if wt.is_dir():
        if session_is_fresh(wt):
            return True, f"gatehouse worktree {wt} has a session updated in the last 30 min"
        return False, f"gatehouse worktree {wt} exists but has no fresh session (stale convoy)"
    res = git_ops._run(
        ["git", "rev-parse", "--verify", "--quiet", f"the-gatehouse/{norm}^{{commit}}"], root
    )
    if res.get("ok") and (res.get("stdout") or "").strip():
        return False, f"branch the-gatehouse/{norm} exists but no gatehouse worktree"
    return False, "no gatehouse worktree or branch found"


_OP_LOCK_DIR = Path("/tmp") / "kilo-castle-op-locks"


def _op_lock_path(name: str) -> Path:
    """Repo-scoped lock path: two different checkouts' courts must not share
    one lock, two windows on the same repo must."""
    import hashlib
    try:
        repo = str(git_ops.get_repo_root())
    except Exception:
        repo = os.environ.get("COURT_DIR") or os.getcwd()
    tag = hashlib.sha1(repo.encode("utf-8")).hexdigest()[:10]
    return _OP_LOCK_DIR / f"{name}-{tag}.lock"


def acquire_op_lock(name: str, stale_seconds: int = 900) -> bool:
    """Advisory cross-window single-writer lock (Class 3: two Steward windows
    double-running collect/ship within seconds). Held via O_EXCL lockfile for
    the remainder of the process lifetime — the OS releases it on exit, so no
    cleanup path is needed. Re-acquiring within the same process is idempotent
    (sequential runs in one process are legitimate); stale locks (holder
    crashed) are stolen after stale_seconds."""
    _OP_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _op_lock_path(name)
    for _attempt in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # Lock present: same process (idempotent), live holder, or stale?
            try:
                holder = lock_path.read_text().strip()
                holder_pid = int(holder or "0")
            except (OSError, ValueError):
                holder, holder_pid = "", 0
            if holder_pid == os.getpid():
                # Re-entrant within this process: touch and succeed.
                try:
                    os.utime(lock_path, None)
                except OSError:
                    pass
                return True
            if holder_pid > 0:
                try:
                    os.kill(holder_pid, 0)
                    return False  # holder alive; genuinely concurrent
                except ProcessLookupError:
                    pass  # holder died: steal, regardless of age
                except PermissionError:
                    return False  # alive under another user
                except OSError:
                    return False
            else:
                # Unparseable pid: fall back to the age grace before stealing.
                try:
                    age = time.time() - lock_path.stat().st_mtime
                except OSError:
                    continue  # vanished between attempts; retry create
                if age < stale_seconds:
                    return False
            try:
                lock_path.unlink()
            except OSError:
                return False
            continue  # retry the exclusive create
        except OSError:
            return False
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True
    return False


def require_op_lock(name: str) -> None:
    if not acquire_op_lock(name):
        try:
            holder = _op_lock_path(name).read_text().strip()
        except OSError:
            holder = "?"
        print(
            f"ERROR: another `{name}` run is already in progress (holder pid {holder}). "
            f"Concurrent operators double-run convoys (cogship-040/041, cogship-247/248, "
            f"the v1332 double-ship). Wait for it to finish instead of racing it.",
            file=sys.stderr,
        )
        sys.exit(1)


def query_kilo_session_ids(worktree_path: Path) -> set[str]:
    """Inspect local kilo.db and return all session IDs recorded for a worktree."""
    db_path = Path.home() / ".local" / "share" / "kilo" / "kilo.db"
    wt_str = str(worktree_path.resolve())
    if not db_path.is_file():
        return set()
    try:
        conn = sqlite3.connect(str(db_path), timeout=1.0)
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM session WHERE directory = ?",
            (wt_str,)
        )
        ids = {row[0] for row in cur.fetchall() if row and row[0]}
        conn.close()
        return ids
    except Exception:
        return set()


def query_latest_kilo_session_id(
    worktree_path: Path,
    timeout_seconds: float = 2.5,
    exclude_ids: Optional[set[str]] = None,
) -> Optional[str]:
    """Inspect local kilo.db to find the session ID created for a worktree.

    When `exclude_ids` is provided (sessions that existed before dispatch),
    prefer a newly created session over a resumed pre-existing one.
    """
    db_path = Path.home() / ".local" / "share" / "kilo" / "kilo.db"
    wt_str = str(worktree_path.resolve())
    deadline = time.time() + timeout_seconds
    while time.time() <= deadline:
        if db_path.is_file():
            try:
                conn = sqlite3.connect(str(db_path), timeout=1.0)
                cur = conn.cursor()
                cur.execute(
                    "SELECT id FROM session WHERE directory = ? ORDER BY time_created DESC",
                    (wt_str,)
                )
                rows = cur.fetchall()
                conn.close()
                excluded = exclude_ids or set()
                new_ids = [r[0] for r in rows if r and r[0] and r[0] not in excluded]
                if new_ids:
                    return new_ids[0]
                if not excluded:
                    if rows and rows[0][0]:
                        return rows[0][0]
            except Exception:
                pass
        time.sleep(0.2)
    return None


def standup_kilo_session(
    worktree_path: Path,
    agent: str,
    model: str,
    prompt: str,
    title: str,
    kilo_bin: Optional[Path] = None,
    server_port: Optional[int] = None,
    server_password: Optional[str] = None,
    run_now: bool = True,
    provider_hint: Optional[str] = None,
    watch_quest: str = "",
) -> dict:
    """Stand up a Kilo session in a worktree with explicit agent mode.

    A detached continuation watchdog (`court watch`) is spawned beside the
    worker: when the single-turn run ends mid-task, the watchdog re-prompts
    the SAME session (generalized goad). `watch_quest` binds the durable
    quest state the watchdog checks for completion.

    Returns dict with keys:
    - 'ok': bool
    - 'mode': 'api' | 'cli' | 'config'
    - 'session_id': str
    - 'message': str
    """
    # 1. Worktree-scoped config (mechanism 4)
    setup_worktree_agent_config(worktree_path, agent)

    # 2. Local Kilo Server HTTP API (mechanism 3)
    port = server_port or os.environ.get("KILO_PORT")
    if port:
        try:
            import urllib.request
            import urllib.error
            url = f"http://127.0.0.1:{port}/session"
            headers = {"Content-Type": "application/json"}
            pw = server_password or os.environ.get("KILO_SERVER_PASSWORD")
            if pw:
                headers["Authorization"] = f"Bearer {pw}"
            req_data = json.dumps({
                "directory": str(worktree_path),
                "agent": agent,
                "title": title,
            }).encode("utf-8")
            req = urllib.request.Request(url, data=req_data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                session_id = resp_data.get("id") or resp_data.get("sessionID")
                if session_id:
                    prompt_url = f"http://127.0.0.1:{port}/session/{session_id}/prompt_async"
                    p_req = urllib.request.Request(
                        prompt_url,
                        data=json.dumps({"prompt": prompt}).encode("utf-8"),
                        headers=headers,
                        method="POST",
                    )
                    urllib.request.urlopen(p_req, timeout=5)
                    return {
                        "ok": True,
                        "mode": "api",
                        "session_id": session_id,
                        "message": f"Stood up session {session_id} on Kilo Server API",
                    }
        except Exception:
            pass

    # 3. Kilo CLI (mechanisms 1 & 2)
    bin_path = kilo_bin or find_kilo_binary()
    task_file = worktree_path / ".kilo" / "TASK.md"
    try:
        task_file.write_text(prompt, encoding="utf-8")
    except Exception:
        pass

    if bin_path and run_now:
        try:
            pre_existing = query_kilo_session_ids(worktree_path)
            qual_model = canonical_model_id(model, provider=config.get_provider(provider_hint or "serf"))
            log_dir = worktree_path / ".kilo"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / f"{agent}.log"

            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"\n--- Launching {agent} session: {title} ({datetime.now().isoformat()}) ---\n")

            log_out = open(log_file, "a", encoding="utf-8")
            try:
                cmd = [
                    str(bin_path),
                    "run",
                    "--agent", agent,
                    "--model", qual_model,
                    "--dir", str(worktree_path),
                    "--title", title,
                    "--auto",
                    prompt,
                ]
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(worktree_path),
                    stdout=log_out,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    env=_engine_spawn_env(worktree_path),
                )
            finally:
                log_out.close()

            session_id = query_latest_kilo_session_id(worktree_path, timeout_seconds=2.5, exclude_ids=pre_existing)
            if session_id:
                watch_pid = _spawn_watchdog(
                    worktree_path, agent, proc.pid, session_id,
                    model=qual_model,
                    quest_ids=watch_quest,
                    task_file="TASK.md",
                    exclude_ids=pre_existing,
                )
                return {
                    "ok": True,
                    "mode": "cli",
                    "session_id": session_id,
                    "pid": proc.pid,
                    "watchdog_pid": watch_pid,
                    "message": f"Stood up session {session_id} (pid {proc.pid}) via Kilo CLI",
                    "log": str(log_file),
                }
            # kilo-coin-36177 class (Q699): the detached process died instantly
            # and the PID-fallback id recorded a session that never existed.
            # poll() -> int is a real exit code; anything else (None/MagicMock
            # in tests) is treated as alive.
            poll_res = proc.poll()
            if isinstance(poll_res, int):
                tail = ""
                try:
                    tail = "\n".join(log_file.read_text(encoding="utf-8").splitlines()[-8:])
                except Exception:
                    pass
                return {
                    "ok": False,
                    "mode": "dead-spawn",
                    "session_id": "",
                    "pid": proc.pid,
                    "message": (
                        f"{agent} process exited immediately (exit {poll_res}); no session was created. "
                        f"Log tail ({log_file}):\n{tail}"
                    ),
                    "log": str(log_file),
                }
            session_id = f"kilo-{agent}-{proc.pid}"
            watch_pid = _spawn_watchdog(
                worktree_path, agent, proc.pid, "",
                model=qual_model,
                quest_ids=watch_quest,
                task_file="TASK.md",
                exclude_ids=pre_existing,
            )
            return {
                "ok": True,
                "mode": "cli",
                "session_id": session_id,
                "pid": proc.pid,
                "watchdog_pid": watch_pid,
                "log": str(log_file),
                "message": f"Spawned background Kilo CLI {agent} session {session_id} (PID {proc.pid})",
            }
        except Exception as e:
            pass

    return {
        "ok": True,
        "mode": "config",
        "session_id": f"kilo-{agent}",
        "message": f"Worktree configured with agent '{agent}' and task written to .kilo/TASK.md",
    }

# Q185 — generalized `cmd_set_field` blocklist (Q183's Humble Opinion: turn
# the small if-chain into a field -> pointer-message map). A field only
# belongs here once a real dedicated command already exists to set it
# correctly — blocking a field with no replacement path would just strand
# callers, which is the opposite of "mechanically enforceable single path".
# Fields flagged by the Q185 Phase 1 audit that do NOT yet have a dedicated
# command (master_of_coin_session_id, gatekeeper_session_id/model,
# vassal_session_id, task_file, cogship_station, cogship_promoted_commit,
# scout_of) are deliberately left settable via raw `set-field` for now — see
# this Quest's Tribute Rendered for the full bucket classification.
DEDICATED_COMMAND_FIELDS: dict[str, str] = {
    "id": "id is immutable; charter a new Quest via `court new` instead.",
    "kind": "kind is immutable; charter a new Quest of the right kind via `court new` instead.",
    "cogship_id": (
        "use the dedicated command: court stamp <id1,id2,...> [--cogship <id>] "
        "(allocates the next unused cogship-NNN when --cogship is omitted)"
    ),
    "status": (
        "use the dedicated command: court advance <id> <STATUS> [--note \"...\"] "
        "(raw set-field bypasses set_status(), the Castle Ledger trail, "
        "and cmd_advance's guards)"
    ),
    "worktree": (
        "use the dedicated command: court dispatch-complete <id> --session-id ID "
        "--branch BRANCH --worktree PATH (records worktree/branch/session/model "
        "together, the way they are always actually known at once)"
    ),
    "serf_session_id": (
        "use the dedicated command: court dispatch-complete <id> --session-id ID "
        "--branch BRANCH --worktree PATH"
    ),
    "serf_model": (
        "use the dedicated command: court dispatch-complete <id> --session-id ID "
        "--branch BRANCH --worktree PATH [--serf-model MODEL] "
        "(default: manifest models.serf in .court/config.json)"
    ),
    "pillory_of": (
        "use the dedicated command: court charter <successor_id> --pillory-of "
        "<predecessor_id> (atomically links both sides: pillory_of on the "
        "successor, pilloried_by on the predecessor, in one commit)"
    ),
    "pilloried_by": (
        "set automatically by `court pillory <id> --successor <successor_id>` or "
        "`court charter <successor_id> --pillory-of <id>` — never set this side "
        "directly, the two fields must stay linked"
    ),
}


def _print_next_steps(header: str, lines: list[str]) -> None:
    """Q185 Universal Targeting Convention: the ONE shared rendering for every
    "here's the exact next command" / "you used the wrong verb, here's the
    right one" reminder. Extracted from the pattern `court charter` (Q183)
    already proved works, so every verb's guidance renders identically
    instead of each one hand-phrasing its own reminder text.
    """
    print()
    print("=" * 76)
    print(header)
    print("=" * 76)
    for line in lines:
        print(line)
    print("=" * 76)


def add_quest_selector(
    parser: argparse.ArgumentParser,
    batchable: bool,
    filters: tuple[str, ...] = ("status", "app", "epic"),
) -> None:
    """Attach the Q185 Universal Targeting Convention to `parser`.

    Every verb that acts on one or more Quests should call this instead of
    hand-rolling its own `add_argument("quest_id", ...)` — one shared
    definition means every verb's `--help` renders the same targeting shape,
    and a cheap model only has to learn the pattern once.

    The POSITIONAL ARGUMENT NAME MECHANICALLY SIGNALS BATCHABILITY, no prose
    required: `quest_ids` (plural) accepts one ID or a comma-separated list;
    `quest_id` (singular) accepts exactly one and `resolve_quest_selection`
    rejects comma input with a clear error.

    `--all` is deliberately never used here — it meant two different things
    on different verbs before this Quest (see Q185's Tribute for the full
    incident writeup): "include archived" and "ignore this verb's narrow
    default --status filter". Those are now two precisely-named flags,
    spelled identically everywhere either concept applies:
      --include-archived   also scan .court/archive/, not just active records
      --any-status          override a narrow default --status filter (only
                             added when "any-status" is in `filters`)
    """
    dest = "quest_ids" if batchable else "quest_id"
    help_txt = (
        "Quest ID, or comma-separated list of IDs (e.g. Q101,Q102). Omit to "
        "select a batch via --status/--app/--epic instead."
        if batchable
        else "Quest ID (exactly one — comma-separated lists are rejected)."
    )
    parser.add_argument(dest, nargs="?", default=None, help=help_txt)
    if "status" in filters:
        # dest is deliberately `status_filter`, not `status` — some verbs
        # (e.g. `advance`) also have their own positional named `status`
        # (the target pipeline stage), and the two must never collide.
        parser.add_argument(
            "--status", dest="status_filter", default=None,
            help="Filter by status (comma-separated)",
        )
    if "app" in filters:
        parser.add_argument("--app", default=None, help="Filter by app domain")
    if "epic" in filters:
        parser.add_argument("--epic", default=None, help="Filter by parent Epic ID")
    if "cogship" in filters:
        parser.add_argument("--cogship", default=None, help="Filter by Cog Ship ID (e.g. cogship-001)")
    parser.add_argument(
        "--include-archived", action="store_true",
        help="Also include archived Quests/Epics from .court/archive/",
    )
    if "any-status" in filters:
        parser.add_argument(
            "--any-status", action="store_true",
            help="Override this command's narrow default --status filter to match every active status",
        )


def resolve_quest_selection(
    args,
    *,
    batchable: bool,
    required: bool = True,
    default_status: Optional[str] = None,
) -> list:
    """Resolve the Quest(s) targeted by an `add_quest_selector` parser.

    One explicit ID (or comma-list, if `batchable`) always wins over filters.
    With no ID(s) given, falls back to `--status`/`--app`/`--epic`/`--cogship`
    /`--include-archived`. `default_status` reproduces a verb's existing
    "narrow default status filter" (e.g. levy's `WORKING,DISPATCHED`) when
    neither an explicit ID nor `--status` was given — bypassed by
    `--any-status`. Errors print to stderr and `sys.exit(1)`, matching every
    existing `cmd_*` function's error convention (never raises).
    """
    raw = getattr(args, "quest_ids", None) or getattr(args, "quest_id", None)
    if raw:
        if not batchable and "," in raw:
            print(
                f"ERROR: this command targets exactly one Quest; got a comma-separated "
                f"list ({raw!r}). Pass a single Quest ID.",
                file=sys.stderr,
            )
            sys.exit(1)
        ids = [s.strip() for s in raw.split(",") if s.strip()]
        quests = []
        for qid in ids:
            try:
                quests.append(store.load(qid))
            except Exception as e:
                print(f"ERROR: {qid}: {e}", file=sys.stderr)
                sys.exit(1)
        return quests

    include_archived = getattr(args, "include_archived", False)
    quests = store.list_all(include_archive=include_archived)

    status_filter = getattr(args, "status_filter", None) or getattr(args, "status", None)
    if not status_filter and default_status and not getattr(args, "any_status", False):
        status_filter = default_status
    if status_filter:
        status_set = {s.strip().upper() for s in status_filter.split(",") if s.strip()}
        quests = [q for q in quests if q.status in status_set]

    app_filter = getattr(args, "app", None)
    if app_filter:
        quests = [q for q in quests if q.app.lower() == app_filter.lower()]

    epic_filter = getattr(args, "epic", None)
    if epic_filter:
        epic_norm = epic_filter.lower().lstrip("q").partition("-")[0]
        quests = [
            q for q in quests
            if q.parent_epic.lower().lstrip("q").partition("-")[0] == epic_norm
            or q.id.lower().lstrip("q").partition("-")[0] == epic_norm
        ]

    cogship_filter = getattr(args, "cogship", None)
    if cogship_filter:
        cog_norm = store.normalize_cogship_id(cogship_filter)
        quests = [q for q in quests if store.normalize_cogship_id(q.cogship_id) == cog_norm]

    if not quests and required:
        print(
            "ERROR: no quest_id(s) given and no filter matched any Quest. "
            "Pass one or more IDs, or a --status/--app/--epic/--cogship filter.",
            file=sys.stderr,
        )
        sys.exit(1)
    return quests


def agent_manager_json_path() -> Path:
    """Resolve `.kilo/agent-manager.json` at the MAIN repository root.

    Agent Manager keeps its state file in the primary checkout, not in linked
    git worktrees (`.kilo/` is per-checkout local state). Engine commands that
    read it (status orphan scan, raze, timber, teardown-list) must therefore
    resolve it via the shared git common dir — otherwise running `court` from
    inside a Quest worktree silently finds nothing. Falls back to the plain
    relative path when git metadata is unavailable.
    """
    local = Path(".kilo/agent-manager.json")
    try:
        res = git_ops._run(["git", "rev-parse", "--git-common-dir"], ".")
        common = (res.get("stdout") or "").strip() if res.get("ok") else ""
        if common:
            common_path = Path(common).resolve()
            candidate = common_path.parent / ".kilo" / "agent-manager.json"
            if candidate.exists():
                return candidate
    except Exception:
        pass
    return local


def _read_last_survey_timestamp() -> str:
    """Parse the last recorded patrol timestamp from the durable Warden's Log."""
    if not WARDENS_LOG_PATH.exists():
        return ""
    for line in WARDENS_LOG_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip().lower().startswith("last survey:"):
            val = line.split(":", 1)[1].strip()
            if val and val.lower() not in ("(none yet)", "none", "-"):
                return val
    return ""


def _list_pending_warden_reports() -> list[Path]:
    """List Warden Reports on disk not yet chartered into a production Quest."""
    if not WARD_REPORTS_DIR.is_dir():
        return []
    return sorted(WARD_REPORTS_DIR.glob("*.md"))


def cmd_init(args):
    from court import init_cmd
    init_cmd.run_init(force=args.force)


def cmd_update(args):
    """Update and sync .court/engine/, templates, commands, prompts, and agents."""
    from court import init_cmd
    init_cmd.run_init(force=True)


def _git_out(repo: Path, *argv: str) -> str:
    res = subprocess.run(["git", *argv], cwd=repo, capture_output=True, text=True)
    return res.stdout if res.returncode == 0 else ""



def _push_file_to_branch(repo: Path, branch: str, rel_path: str, content: str) -> tuple[bool, str]:
    """Commit ONE file's content onto a branch without a working tree: a
    temporary git index is seeded from the branch tree, the file blob replaces
    its entry (nested paths included), then write-tree/commit-tree/update-ref
    move the branch ref. Used by `court sync` to push a royal castle ruling
    (PUNISHED/HELD side-state) DOWN onto the quest branch so worktree roles
    read authoritative state — without folding branch work into castle and
    without the fast-forward assumption a plain `git push .` would need."""
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "court", "GIT_AUTHOR_EMAIL": "court@castle",
           "GIT_COMMITTER_NAME": "court", "GIT_COMMITTER_EMAIL": "court@castle"}
    old_parent = _git_out(repo, "rev-parse", branch).strip()
    if not old_parent:
        return False, f"branch {branch} unresolvable"

    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=str(repo), input=content.encode("utf-8"),
        capture_output=True, timeout=30,
    )
    if blob.returncode != 0:
        return False, (blob.stderr or b"").decode(errors="replace").strip() or "hash-object failed"
    blob_sha = blob.stdout.decode().strip()

    mode_out = _git_out(repo, "ls-tree", branch, "--", rel_path).split()
    mode = mode_out[0] if mode_out else "100644"

    fd, index_path = tempfile.mkstemp(prefix="court-sync-idx-")
    os.close(fd)
    try:
        idx_env = {**env, "GIT_INDEX_FILE": index_path}
        r1 = subprocess.run(["git", "read-tree", branch], cwd=str(repo), env=idx_env, capture_output=True, timeout=30)
        if r1.returncode != 0:
            return False, (r1.stderr or b"").decode(errors="replace").strip() or "read-tree failed"
        r2 = subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", f"{mode},{blob_sha},{rel_path}"],
            cwd=str(repo), env=idx_env, capture_output=True, timeout=30,
        )
        if r2.returncode != 0:
            return False, (r2.stderr or b"").decode(errors="replace").strip() or "update-index failed"
        r3 = subprocess.run(["git", "write-tree"], cwd=str(repo), env=idx_env, capture_output=True, timeout=30)
        if r3.returncode != 0:
            return False, (r3.stderr or b"").decode(errors="replace").strip() or "write-tree failed"
        tree_sha = r3.stdout.decode().strip()
    finally:
        try:
            os.unlink(index_path)
        except OSError:
            pass

    commit = subprocess.run(
        ["git", "commit-tree", tree_sha, "-p", old_parent, "-m",
         f"court: sync royal side-state ruling down to {branch}"],
        cwd=str(repo), env=env, capture_output=True, timeout=30,
    )
    if commit.returncode != 0:
        return False, (commit.stderr or b"").decode(errors="replace").strip() or "commit-tree failed"
    commit_sha = commit.stdout.decode().strip()
    res = subprocess.run(
        ["git", "update-ref", f"refs/heads/{branch}", commit_sha, old_parent],
        cwd=str(repo), capture_output=True, timeout=30,
    )
    if res.returncode != 0:
        return False, (res.stderr or b"").decode(errors="replace").strip() or "update-ref failed"
    git_ops.clear_git_cache()
    return True, commit_sha[:12]


def _union_events(branch_text: str, castle_text: str) -> str:
    """Union two diverged .events.jsonl streams: keep every line from both
    (deduped), stably ordered by ts so the fold's last-by-ts rule resolves
    the effective state. Branch lines win equal-ts ties (castle extras are
    appended after the branch stream before the stable sort)."""
    b_lines = [ln for ln in (branch_text or "").splitlines() if ln.strip()]
    c_lines = [ln for ln in (castle_text or "").splitlines() if ln.strip()]
    if not c_lines:
        return branch_text or ""
    seen = set(b_lines)
    extras = [ln for ln in c_lines if ln not in seen]
    if not extras:
        return branch_text or ""
    merged = b_lines + extras

    def _ts(ln: str) -> str:
        try:
            return str(json.loads(ln).get("ts", ""))
        except Exception:
            return ""

    merged.sort(key=_ts)
    return "\n".join(merged) + "\n"


def _union_merge_quest(base_md: str, castle_md: str, branch_md: str):
    """Three-way section-level union of diverged charter paperwork.

    Per the established conflict policy (charter paperwork -> branch-wins),
    a section only conflicts when BOTH sides changed it to DIFFERENT content
    relative to the merge-base. Castle-only changes (dispatch stamps, master
    charter advances) and branch-only changes (tribute, ledger prose) both
    survive. Returns (merged_text | None, conflicts, disclosures)."""
    from court import models as _models
    try:
        base_q = _models.Quest.from_markdown(base_md or "")
        castle_q = _models.Quest.from_markdown(castle_md or "")
        branch_q = _models.Quest.from_markdown(branch_md or "")
    except ValueError:
        return None, ["not in modern charter shape"], []

    fields_out = {}
    for f in _models.FRONTMATTER_FIELDS:
        bv = str(getattr(branch_q, f, "") or "")
        cv = str(getattr(castle_q, f, "") or "")
        basev = str(getattr(base_q, f, "") or "")
        fields_out[f] = cv if (bv == basev and cv != basev) else bv

    section_keys = []
    for src in (base_q.body_sections, castle_q.body_sections, branch_q.body_sections):
        for k in src:
            if k not in section_keys:
                section_keys.append(k)

    conflicts, disclosures = [], []
    sections_out = {}
    for section in section_keys:
        basev = (base_q.body_sections.get(section, "") or "").strip()
        cv = (castle_q.body_sections.get(section, "") or "").strip()
        bv = (branch_q.body_sections.get(section, "") or "").strip()
        if cv == basev:
            sections_out[section] = bv
        elif bv == basev:
            if cv:
                disclosures.append(f"section '{section}': castle-side change kept")
            sections_out[section] = cv
        elif bv == cv:
            sections_out[section] = bv
        else:
            conflicts.append(section)
    if conflicts:
        return None, conflicts, disclosures
    quest = _models.Quest(**fields_out, body_sections=sections_out)
    return quest.to_markdown(), [], disclosures


def cmd_sync(args):
    """Pull branch-tip quest paperwork into the main checkout.

    The manual half of the ledger single-source-of-truth fix (punished Q696):
    serf self-advances and coin audits commit paperwork on the quest branch,
    while the board and `collect` read the main checkout's ledger — the two
    fork silently. For each quest this reads the committed branch tip (never
    the worktree working tree), compares it against the main checkout's
    copies, and applies the established conflict policy:

      - branch changed, main checkout unchanged since the merge-base
            -> branch-wins: charter + events copied onto the main checkout.
      - main checkout changed, branch unchanged
            -> castle-ahead: nothing to do.
      - both changed
            -> section-level union: branch paperwork wins conflicts,
               castle-only changes survive; only a true edit war (both sides
               rewrote the SAME section differently) refuses.
    """
    repo = git_ops.get_repo_root()
    quests_dir = store.get_quests_dir()
    base = getattr(args, "base", None) or "castle"

    ids = [x for x in (args.quest_ids or []) if x]
    if getattr(args, "all", False):
        ids = [p.stem for p in sorted(quests_dir.glob("*.md"))]
        ids = [i for i in ids if not i.endswith(".events")]
    if not ids:
        if getattr(args, "all", False):
            # A cron-driven `sync --all` on an empty ledger is normal, not an
            # error (the engine repo itself keeps only events files).
            print("Ledger sync: ledger has no quest files — nothing to sync")
            return
        print("No quest ids given (usage: court sync <id> [<id>...] | --all)")
        sys.exit(2)

    synced, united, clean, ahead, mixed, missing, frozen, pushed = [], [], [], [], [], [], [], []
    staged: list[Path] = []
    for qid in ids:
        qpath = store.find_path(qid, quests_dir.parent)
        if not qpath or not qpath.exists():
            missing.append(qid)
            print(f"  ? {qid}: quest file not found in the main checkout")
            continue
        rel = qpath.relative_to(repo).as_posix()
        rel_events = rel[: -len(".md")] + ".events.jsonl"
        head_fm = qpath.read_text(encoding="utf-8")[:2000]
        m = re.search(r"^branch:\s*(\S+)", head_fm, re.MULTILINE)
        branch = m.group(1) if m else ""
        if not branch:
            print(f"  - {qpath.stem}: no branch in frontmatter — skipping")
            continue
        if not _git_out(repo, "rev-parse", "--verify", "--quiet", branch):
            print(f"  - {qpath.stem}: branch {branch} not found — skipping")
            continue

        # Direction guard (Q707-era roll-up failures): a castle PUNISHED/HELD
        # ruling is royal authority — NEVER fold a branch tip over it, even a
        # branch that also changed. The ruling is pushed DOWN onto the branch
        # instead so worktree roles read the authoritative state (only the
        # side-state stamp travels, never a fold of work).
        try:
            castle_quest = Quest.from_markdown(qpath.read_text(encoding="utf-8"))
        except ValueError:
            castle_quest = None
        if castle_quest is not None and castle_quest.status in ("PUNISHED", "HELD"):
            b_show = _git_out(repo, "show", f"{branch}:{rel}")
            try:
                b_status = Quest.from_markdown(b_show).status if b_show else ""
            except ValueError:
                b_status = ""
            if b_show and b_status != castle_quest.status:
                ok, detail = _push_file_to_branch(repo, branch, rel, qpath.read_text(encoding="utf-8"))
                if ok:
                    pushed.append(qpath.stem)
                    print(f"  ⚓ {qpath.stem}: castle side-state [{castle_quest.status}] is royal — branch NOT folded; ruling committed to {branch}")
                else:
                    print(f"  ⚓ {qpath.stem}: castle side-state [{castle_quest.status}] kept (branch fold refused); push-down failed: {detail}")
            else:
                print(f"  ⚓ {qpath.stem}: castle side-state [{castle_quest.status}] is royal — nothing to fold")
            frozen.append(qpath.stem)
            continue

        b_md = _git_out(repo, "show", f"{branch}:{rel}")
        b_events = _git_out(repo, "show", f"{branch}:{rel_events}")
        c_md = qpath.read_text(encoding="utf-8")
        c_events_p = qpath.with_suffix(".events.jsonl")
        c_events = c_events_p.read_text(encoding="utf-8") if c_events_p.exists() else ""

        if b_md == c_md and b_events == c_events:
            clean.append(qpath.stem)
            print(f"  = {qpath.stem}: clean (branch tip matches the main checkout)")
            continue

        mb = _git_out(repo, "merge-base", base, branch).strip()
        paths = [rel] + ([rel_events] if c_events or b_events else [])
        castle_changed = bool(_git_out(repo, "diff", "--name-only", f"{mb}..{base}", "--", *paths).strip()) if mb else True
        branch_changed = bool(_git_out(repo, "diff", "--name-only", f"{mb}..{branch}", "--", *paths).strip()) if mb else True

        if castle_changed and branch_changed:
            base_md = _git_out(repo, "show", f"{mb}:{rel}") if mb else ""
            merged_md, conflicts, disclosures = _union_merge_quest(base_md, c_md, b_md)
            if merged_md is None:
                mixed.append(qpath.stem)
                print(f"  ✗ {qpath.stem}: MIXED fork — both sides rewrote the same sections; refusing (resolve manually)")
                for csection in conflicts[:4]:
                    print(f"      conflicting section: {csection}")
                continue
            qpath.write_text(merged_md, encoding="utf-8")
            staged.append(qpath)
            merged_events = _union_events(b_events, c_events)
            qpath.with_suffix(".events.jsonl").write_text(merged_events, encoding="utf-8")
            staged.append(qpath.with_suffix(".events.jsonl"))
            united.append(qpath.stem)
            print(f"  ⊕ {qpath.stem}: mixed fork united (branch paperwork + castle-only changes)")
            for d in disclosures[:4]:
                print(f"      {d}")
            continue
        if castle_changed and not branch_changed:
            ahead.append(qpath.stem)
            # Castle-ahead sync-down (Q707: an hour of stale charters in the
            # worktree): dispatch records and M'Lord's amendments live on
            # castle only — push the charter DOWN into the quest worktree and
            # commit it on the branch so the serf reads the current charter.
            wt_dir = Path(castle_quest.worktree) if (castle_quest and castle_quest.worktree) else None
            if wt_dir and wt_dir.is_dir():
                dirty = subprocess.run(
                    ["git", "status", "--porcelain"], cwd=str(wt_dir),
                    capture_output=True, text=True, timeout=30,
                ).stdout.strip()
                if dirty:
                    print(f"  > {qpath.stem}: castle-ahead — worktree dirty, charter NOT pushed down (don't stomp live work)")
                else:
                    wt_charter = wt_dir / ".court" / "quests" / qpath.name
                    try:
                        wt_charter.parent.mkdir(parents=True, exist_ok=True)
                        wt_charter.write_text(c_md, encoding="utf-8")
                        wt_events = wt_charter.with_suffix(".events.jsonl")
                        if c_events:
                            wt_events.write_text(c_events, encoding="utf-8")
                        add = subprocess.run(
                            ["git", "add", "--", wt_charter.name] + ([wt_events.name] if c_events else []),
                            cwd=str(wt_charter.parent), capture_output=True, timeout=30,
                        )
                        commit = subprocess.run(
                            ["git", "commit", "-m", f"court: sync castle charter down to worktree ({qpath.stem})"],
                            cwd=str(wt_dir), capture_output=True, timeout=60,
                            env={**os.environ,
                                 "GIT_AUTHOR_NAME": "court", "GIT_AUTHOR_EMAIL": "court@castle",
                                 "GIT_COMMITTER_NAME": "court", "GIT_COMMITTER_EMAIL": "court@castle"},
                        )
                    except OSError as e:
                        add = commit = None
                        print(f"  > {qpath.stem}: castle-ahead — sync-down failed: {e}")
                    commit_out = "" if commit is None else ((commit.stdout or b"") + (commit.stderr or b"")).decode(errors="replace")
                    if add is not None and add.returncode != 0:
                        print(f"  > {qpath.stem}: castle-ahead — sync-down add failed: {(add.stderr or b'').decode(errors='replace').strip()[:200]}")
                    elif commit is not None and commit.returncode == 0:
                        pushed.append(qpath.stem)
                        git_ops.clear_git_cache()
                        print(f"  ⇣ {qpath.stem}: castle-ahead charter synced DOWN to the worktree (dispatch records/amendments)")
                    elif commit is not None and "nothing to commit" in commit_out:
                        print(f"  > {qpath.stem}: castle-ahead — worktree charter already current")
                    elif commit is not None:
                        print(f"  > {qpath.stem}: castle-ahead — sync-down commit failed: {commit_out.strip()[:200]}")
            else:
                print(f"  > {qpath.stem}: castle-ahead — main checkout already newer, nothing to do")
            continue

        # branch strictly ahead: branch-wins for charter paperwork
        qpath.write_text(b_md, encoding="utf-8")
        staged.append(qpath)
        if b_events:
            qpath.with_suffix(".events.jsonl").write_text(b_events, encoding="utf-8")
            staged.append(qpath.with_suffix(".events.jsonl"))
        elif c_events:
            print(f"      note: branch carries no events file; kept the main checkout's events")
        synced.append(qpath.stem)
        print(f"  ✓ {qpath.stem}: synced branch paperwork onto the main checkout (branch-wins)")

    if staged:
        msg = "court: sync quest ledger from branch — " + ", ".join(synced[:6]) + ("…" if len(synced) > 6 else "")
        res = git_ops.git_commit_paths(staged, msg, cwd=repo)
        if not res.get("ok", True):
            print(f"  commit failed: {res}")
            sys.exit(1)
    print()
    print(f"Ledger sync: {len(synced)} synced, {len(united)} united, {len(clean)} clean, {len(ahead)} castle-ahead, {len(frozen)} side-state-royal ({len(pushed)} ruling(s) pushed down), {len(mixed)} MIXED, {len(missing)} not found")
    if mixed:
        print(f"  Unresolvable edit wars need manual resolution: {', '.join(mixed)}")
        if getattr(args, "all", False):
            print("  (global sync: edit wars are reported, not fatal — resolve them by hand)")
        else:
            sys.exit(1)


def cmd_ward(args):
    last_survey = _read_last_survey_timestamp()
    pending_reports = _list_pending_warden_reports()
    realm = ward.audit_realm(base_branch=getattr(args, "base", "castle") or "castle")
    non_compliant = [a for a in realm["audits"] if not a.is_compliant]

    if getattr(args, "json", False):
        out = {
            "last_survey": last_survey or None,
            "pending_warden_reports": [str(p) for p in pending_reports],
            "realm_compliance": {
                "total_active": realm["total_active"],
                "compliant_count": realm["compliant_count"],
                "non_compliant_count": realm["non_compliant_count"],
                "dirty_count": realm["dirty_count"],
                "behind_count": realm["behind_count"],
                "non_compliant_ids": [a.quest_id for a in non_compliant],
            },
        }
        print(json.dumps(out, indent=2))
        return

    print("=" * 76)
    print("🏹 THE WARD — HUNTING GROUNDS PATROL & REALM COMPLIANCE")
    print("=" * 76)
    print(f"Last Survey: {last_survey or '(never surveyed — no patrol has run yet)'}")
    print()

    print(f"📋 WARDEN REPORTS AWAITING CHARTER ({len(pending_reports)})")
    if pending_reports:
        for p in pending_reports:
            print(f"  - {p.name}")
    else:
        print("  (none pending)")
    print()

    print(f"🛡️ REALM COMPLIANCE HEALTH: {realm['compliant_count']}/{realm['total_active']} active Quests compliant")
    print(f"  Dirty worktrees: {realm['dirty_count']} | Behind castle: {realm['behind_count']}")
    if non_compliant:
        for a in non_compliant:
            print(f"  🔴 {a.quest_id}: {a.title}")
            for v in a.violations:
                print(f"      - {v}")
    print()
    print("=" * 76)


def cmd_new(args):
    quest_id = store.make_id(args.app, args.concern)
    quest = Quest(
        id=quest_id,
        title=args.title,
        kind=args.kind,
        app=args.app,
        concern=args.concern,
        section=args.section or "",
        tags=args.tags or args.section or "",
        parent_epic=args.epic or "",
        scout_of=getattr(args, "scout_of", "") or "",
    )
    branch_candidate = args.branch or quest.tree_branch
    is_valid, err = validate_branch_name(branch_candidate)
    if not is_valid:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)
    quest.branch = branch_candidate
    if args.goal:
        quest.set_section("The Kingdom Requires", args.goal)
    # Standing checklist item on every Quest, regardless of Steward-authored
    # content: the disk-durable anchor for the "you must self-advance" rule.
    # A Serf's own chat context can compact/reset mid-Quest the same way the
    # Steward's can -- but this file, sitting on disk right where they call
    # set-section, does not. See AGENTS.md Gate 2 / LEDGER.md 2026-09-05.
    self_advance_reminder = (
        f"- [ ] **Self-advance (do this LAST)**: once Tribute is rendered and "
        f"`git rev-list --count HEAD..castle` is 0, run "
        f"`python3 -m court.cli advance {quest_id} TRIBUTE_READY` yourself. "
        f"Nothing else flips the status out of WORKING."
    )
    tribute_content = args.tribute or ""
    combined_tribute = (
        f"{tribute_content}\n\n{self_advance_reminder}" if tribute_content else self_advance_reminder
    )
    quest.set_section("Expected Tribute", combined_tribute)
    quest.log_ledger("-", quest.status, "Quest created")
    auto_commit = not getattr(args, "no_commit", False)
    path = store.save(quest, auto_commit=auto_commit, commit_msg=f"court: create {quest.id}")
    print(f"Created {quest.id} -> {path}")
    print(quest.to_markdown())


STATUS_GLYPHS = {
    "OPEN": "📋",
    "PLANNED": "📝",
    "CHARTERED": "📜",
    "DISPATCHED": "📜",
    "QUESTING": "⚔️",
    "WORKING": "⚔️",
    "TRIBUTE_READY": "🪙",
    "GATE": "🛡️",
    "READY_TO_RAZE": "🪦",
    "LANDED": "🚢",
    "LAUNCHED": "🏰",
    "DONE": "🚢",
    "HELD": "⏸️",
    "PUNISHED": "🔒",
    "DEMOTED": "👇",
}


def cmd_show(args):
    quest = store.load(args.quest_id)
    section = getattr(args, "section", None)
    if section:
        content = quest.extract_tribute_subsection(section)
        if not content:
            content = quest.body_sections.get(section, "")
        if content:
            print(f"### {quest.id} ({quest.title}) [{quest.status}] — {section.upper()}\n")
            print(content)
        else:
            print(f"(no section {section!r} found in {quest.id})")
        return

    print(quest.to_markdown())
    if quest.kind == "epic":
        hierarchy = store.get_hierarchy(include_archive=False)
        for epic, children in hierarchy["epics"]:
            if epic.id == quest.id or epic.id.split("-")[0].lower() == quest.id.split("-")[0].lower():
                if children:
                    completed = sum(1 for c in children if c.status in ("READY_TO_RAZE", "DONE"))
                    print(f"\n# Child Quests ({completed}/{len(children)} Complete)\n")
                    for j, child in enumerate(children):
                        is_last = (j == len(children) - 1)
                        pfx = "└── " if is_last else "├── "
                        glyph = STATUS_GLYPHS.get(child.status, "•")
                        serf = f"serf={child.serf_session_id}" if child.serf_session_id else ""
                        branch = f"branch={child.branch}" if child.branch else ""
                        meta = " ".join(filter(None, [branch, serf]))
                        meta_str = f" ({meta})" if meta else ""
                        print(f"{pfx}{glyph} [{status_label(child.status)}] {child.id}{meta_str}")
                        print(f"{'    ' if is_last else '│   '}    {child.title}")
                break


def cmd_worktree_doc(args):
    quest = store.load(args.quest_id)
    wt = git_ops.find_worktree_for_quest(quest)
    if not wt or not wt.is_dir():
        print(f"ERROR: No active worktree found for {quest.id} (branch: {quest.branch or '-'})", file=sys.stderr)
        sys.exit(1)

    candidate_paths = [
        wt / ".court" / "quests" / f"{quest.id}.md",
        wt / ".court" / "epics" / f"{quest.id}.md",
    ]
    short_num = quest.id.split("-")[0].lstrip("Qq")
    prefix = f"Q{short_num}-"
    for d in (wt / ".court" / "quests", wt / ".court" / "epics"):
        if d.is_dir():
            candidate_paths.extend(list(d.glob(f"{prefix}*.md")))

    src_file = None
    for cp in candidate_paths:
        if cp.exists() and cp.is_file():
            src_file = cp
            break

    if not src_file:
        print(f"ERROR: No quest markdown found inside worktree {wt}", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "path", False):
        print(src_file)
        return

    wt_content = src_file.read_text(encoding="utf-8")
    castle_path = store.find_path(quest.id)
    castle_content = castle_path.read_text(encoding="utf-8") if castle_path and castle_path.exists() else ""

    if getattr(args, "json", False):
        import json
        print(json.dumps({
            "quest_id": quest.id,
            "worktree_path": str(wt),
            "document_path": str(src_file),
            "content": wt_content,
            "modified_from_castle": wt_content != castle_content,
        }, indent=2))
        return

    if getattr(args, "diff", False):
        import difflib
        diff = list(difflib.unified_diff(
            castle_content.splitlines(keepends=True),
            wt_content.splitlines(keepends=True),
            fromfile=f"castle/{castle_path.name if castle_path else quest.id + '.md'}",
            tofile=f"worktree/{src_file.name}",
        ))
        if diff:
            print("".join(diff))
        else:
            print("No difference between castle and worktree quest documents.")
        return

    if getattr(args, "sync", False):
        success, msg = ward.sync_tribute_from_worktree(quest, worktree_path=wt)
        print(f"Sync result for {quest.id}: {msg}")
        return

    print(f"=========================================================================")
    print(f"WORKTREE TASK DOCUMENT: {quest.id} ({src_file})")
    print(f"=========================================================================")
    print(wt_content)


def cmd_list(args):
    quests = resolve_quest_selection(args, batchable=True, required=False)
    if not quests:
        print("(no matching quests)")
        return
    for q in quests:
        print(f"{q.id:38} [{status_label(q.status):19}] {q.section:12} {q.title[:60]}")


def render_tree_view(include_archive: bool = False) -> str:
    hierarchy = store.get_hierarchy(include_archive=include_archive)
    epics = hierarchy["epics"]
    standalone = hierarchy["standalone"]
    scouts = hierarchy["scouts"]

    lines = [
        "=" * 72,
        "THE COURT — Quest & Epic Hierarchy",
        "=" * 72,
    ]

    if epics:
        lines.append(f"\n🏰 EPICS ({len(epics)})")
        for i, (epic, children) in enumerate(epics):
            is_last_epic = (i == len(epics) - 1) and not standalone and not scouts
            prefix = "└── " if is_last_epic else "├── "
            indent = "    " if is_last_epic else "│   "

            completed_count = sum(1 for c in children if c.status in ("READY_TO_RAZE", "DONE"))
            total_count = len(children)
            progress = f"({completed_count}/{total_count} Quests Complete)" if total_count else "(No child Quests)"
            glyph = STATUS_GLYPHS.get(epic.status, "•")

            lines.append(f"{prefix}{glyph} [{status_label(epic.status)}] {epic.id} {progress}")
            lines.append(f"{indent}    {epic.title}")

            for j, child in enumerate(children):
                is_last_child = (j == len(children) - 1)
                c_prefix = "└── " if is_last_child else "├── "
                c_glyph = STATUS_GLYPHS.get(child.status, "•")
                c_serf = f"serf={child.serf_session_id}" if child.serf_session_id else ""
                c_branch = f"branch={child.branch}" if child.branch else ""
                task_prog = ward.get_worktree_task_progress(quest=child, app=child.app, quest_id=child.id)
                task_str = f"Tasks: {task_prog['summary']}" if task_prog.get("found") else ""
                meta = " ".join(filter(None, [task_str, c_branch, c_serf]))
                meta_str = f" ({meta})" if meta else ""
                lines.append(f"{indent}{c_prefix}{c_glyph} [{status_label(child.status)}] {child.id}{meta_str}")
                lines.append(f"{indent}{'    ' if is_last_child else '│   '}    {child.title}")

    if standalone:
        lines.append(f"\n⚔️ STANDALONE QUESTS ({len(standalone)})")
        for i, q in enumerate(standalone):
            is_last = (i == len(standalone) - 1) and not scouts
            prefix = "└── " if is_last else "├── "
            indent = "    " if is_last else "│   "
            glyph = STATUS_GLYPHS.get(q.status, "•")
            serf = f"serf={q.serf_session_id}" if q.serf_session_id else ""
            branch = f"branch={q.branch}" if q.branch else ""
            task_prog = ward.get_worktree_task_progress(quest=q, app=q.app, quest_id=q.id)
            task_str = f"Tasks: {task_prog['summary']}" if task_prog.get("found") else ""
            sec_str = f"[{q.section}]" if q.section else ""
            meta = " ".join(filter(None, [task_str, branch, serf]))
            meta_str = f" ({meta})" if meta else ""
            lines.append(f"{prefix}{glyph} [{status_label(q.status)}] {q.id} {sec_str}{meta_str}".replace("  ", " "))
            lines.append(f"{indent}    {q.title}")

    if scouts:
        lines.append(f"\n🔭 SCOUTS & INVESTIGATIONS ({len(scouts)})")
        for i, q in enumerate(scouts):
            is_last = (i == len(scouts) - 1)
            prefix = "└── " if is_last else "├── "
            indent = "    " if is_last else "│   "
            glyph = STATUS_GLYPHS.get(q.status, "•")
            serf = f"serf={q.serf_session_id}" if q.serf_session_id else ""
            branch = f"branch={q.branch}" if q.branch else ""
            task_prog = ward.get_worktree_task_progress(quest=q, app=q.app, quest_id=q.id)
            task_str = f"Tasks: {task_prog['summary']}" if task_prog.get("found") else ""
            meta = " ".join(filter(None, [task_str, branch, serf]))
            meta_str = f" ({meta})" if meta else ""
            lines.append(f"{prefix}{glyph} [{status_label(q.status)}] {q.id}{meta_str}")
            lines.append(f"{indent}    {q.title}")

    return "\n".join(lines)


def cmd_tree(args):
    print(render_tree_view(include_archive=getattr(args, "include_archived", False)))


def find_orphaned_worktrees() -> list[dict]:
    """Cross-reference live Agent Manager worktrees against ACTIVE Court Quests.

    A worktree whose branch has no matching active Quest record is an orphan —
    most commonly a Quest (often a Scout) whose record was archived while its
    physical worktree/branch stayed live in Agent Manager, so it silently
    vanishes from `court status`'s default (active-only) view forever.
    """
    import json
    am_path = agent_manager_json_path()
    if not am_path.exists():
        return []
    try:
        am_data = json.loads(am_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    active_quests = store.list_all(include_archive=False)
    active_branches = {q.branch for q in active_quests if q.branch}
    all_quests = store.list_all(include_archive=True)
    branch_to_quest = {q.branch: q for q in all_quests if q.branch}

    orphans = []
    for wdata in am_data.get("worktrees", {}).values():
        branch = wdata.get("branch") or ""
        if not branch or branch in active_branches:
            continue
        # Persistent infrastructure branches are never Quest-linked by design.
        if branch in ("main", "castle") or branch.startswith("the-gatehouse"):
            continue
        matched = branch_to_quest.get(branch)
        orphans.append({
            "branch": branch,
            "path": wdata.get("path", ""),
            "quest_id": matched.id if matched else None,
            "quest_status": matched.status if matched else None,
        })
    return orphans


def _last_ledger_entry_is_forced(quest: Quest) -> bool:
    """Q185: a forced status transition (`advance ... --force`) must never
    look identical to a real one on the live dashboard — the 2026-09-05
    incident's actual damage was that a FORCED note only ever lived in full
    Castle Ledger history, invisible to anyone skimming `court status`'s
    bucket view. Checks whether the LAST bulleted Castle Ledger entry (the
    one that produced this Quest's current status) contains '(FORCED'.
    """
    ledger = quest.body_sections.get("Castle Ledger", "")
    for line in reversed(ledger.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        return "(FORCED" in stripped
    return False


def cmd_status(args):
    if getattr(args, "sync", False):
        all_active = store.list_all(include_archive=False)
        for q in all_active:
            if q.status in ("QUESTING", "WORKING", "CHARTERED", "DISPATCHED", "TRIBUTE_READY", "GATE"):
                ward.sync_tribute_from_worktree(q)

    if getattr(args, "tree", False):
        print(render_tree_view(include_archive=getattr(args, "include_archived", False)))
        return

    quests = resolve_quest_selection(args, batchable=True, required=False)
    if getattr(args, "scouts", False):
        quests = [q for q in quests if q.kind == "scout" or q.section == "Investigation"]

    if getattr(args, "json", False):
        results = []
        for q in quests:
            audit = ward.audit_quest(q, base_branch="castle")
            q_dict = {
                "id": q.id,
                "title": q.title,
                "kind": q.kind,
                "app": q.app,
                "concern": q.concern,
                "section": q.section,
                "tags": q.tags,
                "status": q.status,
                "branch": q.branch,
                "worktree": audit.worktree_path,
                "serf_session_id": q.serf_session_id,
                "serf_model": q.serf_model,
                "master_of_coin_session_id": q.master_of_coin_session_id,
                "gatekeeper_session_id": q.gatekeeper_session_id,
                "parent_epic": q.parent_epic,
                "scout_of": getattr(q, "scout_of", ""),
                "cogship_id": q.cogship_id or "",
                "is_compliant": audit.is_compliant,
                "git_status": audit.git_status,
                "task_progress": audit.task_progress,
                "tribute_present": audit.tribute_present,
                "sections_present": audit.sections_present,
                "missing_sections": audit.missing_sections,
                "violations": audit.violations,
                "warnings": audit.warnings,
                "forced_transition": _last_ledger_entry_is_forced(q),
                "commutation": q.extract_commutation(),
                "commutation_done": q.commutation_complete(),
                "pending_audience": q.has_pending_audience(),
            }
            results.append(q_dict)
        out = {
            "quests": results,
            "summary": {
                "total": len(quests),
                "by_status": {s: len([q for q in quests if q.status == s]) for s in STATUSES},
            },
        }
        print(json.dumps(out, indent=2))
        return

    if not quests:
        print("The Court is empty. No active Quests or Epics.")
        return

    def _render_item(q: Quest) -> None:
        is_scout = (q.kind == "scout" or q.section == "Investigation")
        marker = "🔭 " if is_scout else ""
        epic_badge = " (Epic)" if q.kind == "epic" else ""
        branch_str = f"({q.branch})" if q.branch else ""
        forced_marker = " ⚠️FORCED" if _last_ledger_entry_is_forced(q) else ""

        if branch_str:
            print(f"{marker}{q.id} {branch_str} — {q.title}{epic_badge}{forced_marker}")
        else:
            print(f"{marker}{q.id} — {q.title}{epic_badge}{forced_marker}")

        if forced_marker:
            print(f"      ⚠️  Last transition was FORCED past a failed check — see Castle Ledger before trusting this status.")
        if getattr(q, "scout_of", ""):
            print(f"      🔗 implements Scout Report from {q.scout_of}")

        audit = ward.audit_quest(q, base_branch="castle")
        tp = audit.task_progress
        gs = audit.git_status

        # Badges order: Phase, Tasks, Tribute, Clean/dirty (no ahead/behind)
        badges = []
        if tp.get("active_phase"):
            badges.append(f"| Phase: {tp['active_phase']}")
        if tp.get("found"):
            badges.append(f"[Tasks: {tp['summary']}]")
        if audit.tribute_present:
            req_total = 5 if is_scout else 6
            present_cnt = len(getattr(audit, "present_sections", getattr(audit, "sections_present", [])))
            badges.append(f"(Tribute: {present_cnt}/{req_total} sections)")
        elif q.status in ("QUESTING", "WORKING", "CHARTERED", "DISPATCHED"):
            badges.append("(Tribute: in-progress)")

        if gs.get("exists"):
            dirty_cnt = len(gs.get("untracked", [])) + len(gs.get("modified", [])) + len(gs.get("staged", [])) + len(gs.get("deleted", []))
            git_badge = f"[DIRTY: {dirty_cnt} files]" if gs.get("dirty") else "[CLEAN]"
            badges.append(git_badge)

        if badges:
            print(f"      {' '.join(badges)}")

        # Print actionable warnings / violations
        if gs.get("dirty"):
            dirty_cnt = len(gs.get("untracked", [])) + len(gs.get("modified", [])) + len(gs.get("staged", [])) + len(gs.get("deleted", []))
            dirty_sample = (gs.get("untracked", []) + gs.get("modified", []) + gs.get("staged", []) + gs.get("deleted", []))[:3]
            print(f"      🔴 Dirty working tree: {dirty_cnt} uncommitted file(s) ({', '.join(dirty_sample)}).")

        behind = gs.get("behind")
        if behind is not None and behind > 0 and q.status in ("TRIBUTE_READY", "GATE", "PUNISHED"):
            print(f"      🔴 Base drift: worktree is {behind} commit(s) behind castle.")

        if q.status in ("TRIBUTE_READY", "GATE") and q.has_pending_audience():
            print(f"      🔴 Pending Serf Audience: Serf documented an open decision in Tribute requiring royal judgment; resolve via /audience.")

    # 1. PLANNING
    planned = [q for q in quests if q.status == "PLANNED"]
    if planned:
        print(f"\n📝 PLANNING ({len(planned)}) keep working with /plan")
        for q in planned:
            _render_item(q)

    # 2. OPEN
    open_quests = [q for q in quests if q.status == "OPEN"]
    if open_quests:
        print(f"\n📋 OPEN ({len(open_quests)})")
        for q in open_quests:
            _render_item(q)

    # 3. CHARTERED
    chartered = [q for q in quests if q.status in ("CHARTERED", "DISPATCHED")]
    if chartered:
        print(f"\n📜 CHARTERED ({len(chartered)}) — ready to start or goad")
        for q in chartered:
            _render_item(q)

    # 4. QUESTING (Always rendered)
    questing = [q for q in quests if q.status in ("QUESTING", "WORKING")]
    print(f"\n⚔️ QUESTING ({len(questing)}) — /goad idle serfs or /levy to conduct tribute audit")
    if questing:
        for q in questing:
            _render_item(q)
    else:
        print("  (no serfs currently questing in the field)")

    # 5. DEMOTED (side-state)
    demoted = [q for q in quests if q.status == "DEMOTED"]
    if demoted:
        print(f"\n👇 DEMOTED ({len(demoted)})")
        for q in demoted:
            _render_item(q)

    # 6. PUNISHED (side-state)
    punished = [q for q in quests if q.status == "PUNISHED"]
    if punished:
        print(f"\n🔒 PUNISHED ({len(punished)}) — frozen side-state; charter successor with /charter <new_id> --pillory-of <id>")
        for q in punished:
            _render_item(q)

    # 7. HELD (side-state)
    held = [q for q in quests if q.status == "HELD"]
    if held:
        print(f"\n⏸️ HELD ({len(held)}) — blocked on /audience")
        for q in held:
            _render_item(q)

    # 8. TRIBUTE_READY (Never "REVIEW")
    tribute_ready = [q for q in quests if q.status in ("TRIBUTE_READY", "REVIEW")]
    if tribute_ready:
        print(f"\n🪙 TRIBUTE_READY (Raising Tribute) ({len(tribute_ready)}) — ready for /levy")
        for q in tribute_ready:
            _render_item(q)

    # 9. GATE
    gate = [q for q in quests if q.status == "GATE"]
    if gate:
        print(f"\n🛡️ Tribute at the GATE ready for /collect ({len(gate)})")
        for q in gate:
            _render_item(q)

    # 10. COGSHIPS READY
    cogship_quests = []
    cogship_candidates = [
        q for q in quests
        if q.status in ("READY_TO_RAZE", "READY_FOR_TEARDOWN", "DONE")
        and q.kind != "scout"
        and q.section != "Investigation"
        and not git_ops.is_quest_merged_into(q, target_ref="main")
    ]
    if cogship_candidates:
        ship_manifest = store.rollup_ship_manifest(quests=cogship_candidates)
        cogship_quests = ship_manifest.get("quests", [])
        if cogship_quests:
            print(f"\n🚢 Cogships Ready ({len(cogship_quests)}) launch with /ship")
            for q in cogship_quests:
                parent_info = f" [Epic: {q.parent_epic}]" if q.parent_epic else ""
                print(f"  - {q.id} ({q.app}): {q.title}{parent_info}")

    # 11. READY_TO_RAZE
    raze_quests = [q for q in quests if q.status == "READY_TO_RAZE"]
    if raze_quests:
        print(f"\n🪦 READY_TO_RAZE ({len(raze_quests)}) — ready for /teardown")
        for q in raze_quests:
            _render_item(q)

    # 12. COMMUTATIONS REQUIRED / DONE
    pending_commutations = []
    done_commutations = []
    for q in quests:
        c = q.extract_commutation()
        if not c:
            continue
        if q.commutation_complete():
            done_commutations.append((q, c))
        else:
            pending_commutations.append((q, c))
    if pending_commutations:
        print(f"\n⚡ COMMUTATIONS REQUIRED ({len(pending_commutations)}) — post-deployment actions for the Steward")
        for q, c in pending_commutations:
            print(f"  - {q.id} ({q.app}): {c}")
    if done_commutations:
        done_ids = ", ".join(q.id for q, _ in done_commutations)
        print(f"\n✅ Commutations Done ({len(done_commutations)}) — logged in Cogship Log: {done_ids}")

    # 13. Footer & Action Prompts
    print("\nHear the quest ballads with /bard /atone /coffers /tally and /murmur")
    if cogship_quests:
        print("\nReady to Ship - Use /ship --confirm to launch")

    orphans = find_orphaned_worktrees()
    if orphans:
        print(f"Note: {len(orphans)} orphaned worktrees exist on disk (court timber / court raze / court fork-teardown-list available for cleanup).")


def cmd_pillory(args):
    """Send a Quest to the pillory (synonym: /punish).

    Unconditional and automatic: no proof of landing means straight to
    PUNISHED with Decrees; a Quest is never returned to WORKING from here.
    The one courtesy check is proof-of-landing — if the work actually did
    land somewhere despite appearances, don't waste a whole new Quest+worktree
    ceremony on it.
    """
    quest = store.load(args.quest_id)
    proof = git_ops.check_proof_of_landing(quest)
    if proof.get("landed"):
        sha = proof.get("proof_commit", "unknown")
        ref = proof.get("matched_ref", "unknown")
        note = f"Pillory check: found proof of landing on {ref} (commit {sha}); no punishment needed."
        quest.log_ledger(quest.status, quest.status, note)
        store.save(quest)
        print(f"✅ {quest.id}: Work found landed on {ref} (commit {sha}). Status remains [{quest.status}] ({status_label(quest.status)}).")
        return

    reason = args.reason or "Master of Coin audit rejected this Quest's tribute."
    decrees = args.decrees or "(no decrees recorded — Steward must supply before chartering the successor)"
    old_status = quest.status
    quest.set_status("PUNISHED", f"Punished: {reason}")
    quest.set_section(
        "Judgement of the Condemned",
        "\n".join([
            f"- **Reason:** {reason}",
            f"- **Decrees Issued:** {decrees}",
            f"- **Successor Quest:** {args.successor or '(pending — charter one now)'}",
            f"- **Frozen Worktree:** {quest.worktree or '-'} (read-only; no Serf re-enters it)",
            f"- **Raze Together With:** {args.successor or '(set once successor is chartered)'} (both razed in one pass once the successor completes)",
        ]),
    )
    if args.successor:
        quest.pilloried_by = args.successor
    store.save(quest)
    print(f"🔒 {quest.id}: Sent to the pillory. Status set to [PUNISHED] ({status_label('PUNISHED')}). Was: [{old_status}].")
    print(f"    Reason: {reason}")
    print(f"    Decrees: {decrees}")
    if not args.successor:
        _print_next_steps(
            f"⚖️ NEXT STEPS — CHARTER A SUCCESSOR FOR {quest.id}",
            [
                "Charter a new Quest with these Decrees seeded into its Kingdom",
                "Requires, then link it back to this pilloried record in ONE command",
                "(never raw `set-field pillory_of`/`pilloried_by` — both fields must",
                "stay linked, which is exactly what this composite guarantees):",
                "",
                f"  python3 -m court.cli charter <new_id> --pillory-of {quest.id}",
            ],
        )


def cmd_advance(args):
    quests = resolve_quest_selection(args, batchable=True, required=True)
    new_status = args.status
    force = getattr(args, "force", False)
    verified_commit = getattr(args, "verified_commit", None)
    auto_commit = not getattr(args, "no_commit", False)
    for quest in quests:

        if new_status == "GATE":
            # Hardened GATE guard: a Quest cannot slip into GATE (Collecting
            # Tribute) with an incomplete/non-compliant Tribute (missing
            # required rollup subsections) or base drift against castle,
            # unless an explicit --force override with a reason is passed.
            # See Q135: Q121 reached GATE with 1/6 tribute sections and 51
            # commits of drift, and nothing blocked it.
            audit = ward.audit_quest(quest)
            if audit.violations and not force:
                violation_lines = "\n".join(f"  🔴 {v}" for v in audit.violations)
                print(
                    f"ERROR: Cannot advance {quest.id} to GATE (Collecting Tribute): "
                    f"compliance audit found {len(audit.violations)} violation(s).\n"
                    f"{violation_lines}\n\n"
                    f"To bypass this check, use: python3 -m court.cli advance {quest.id} GATE --force --note \"<reason>\"",
                    file=sys.stderr,
                )
                sys.exit(1)
            elif audit.violations:
                forced_note = (
                    f"(FORCED - {len(audit.violations)} COMPLIANCE VIOLATION(S): "
                    f"{'; '.join(audit.violations)}) {args.note}"
                ).strip()
                quest.set_status(new_status, forced_note)
            else:
                quest.set_status(new_status, args.note or "")

        elif new_status == "TRIBUTE_READY":
            # Hardened TRIBUTE_READY guard (Q277/Q696 class): the most-used
            # transition ran on a bare else — no tribute-completeness, no
            # unchecked-checklist, no base-drift check — so quests like Q277
            # self-advanced with an empty Tribute Rendered, unchecked Expected
            # Tribute boxes, and 187 commits of drift. GATE and READY_TO_RAZE
            # already carry hardened, --force-marked guards; TRIBUTE_READY now
            # matches them. ward.audit_quest only flags tribute gaps once the
            # quest is *already* TRIBUTE_READY+, so completeness is checked
            # here explicitly with ward.check_tribute_sections (status-free).
            tribute_present, _present_secs, missing_secs, _extracted = ward.check_tribute_sections(quest)
            blockers: list[str] = []
            if not tribute_present:
                blockers.append(
                    "Tribute Rendered is empty or a placeholder — render the required "
                    "sections (Ballad, Tribute, Tally, Penance, Audience, Opinion; scouts: "
                    "Survey, Map, Dangers, Tribute, Plot) before advancing."
                )
            elif missing_secs:
                blockers.append(
                    f"Incomplete tribute: missing required subsection(s): {', '.join(missing_secs)}."
                )

            checklist = ward.parse_markdown_checklist(quest.body_sections.get("Expected Tribute", ""))
            # Hotfix fast-track exemption: patchwork quests carry an advisory
            # checklist and promote via inline verification (the hotfix collect
            # auto-advance never checked it either); the hard checklist gate
            # would block the exact flow the hotfix doctrine prescribes.
            is_hotfix = bool(getattr(quest, "is_hotfix", False)) or "hotfix" in (quest.tags or "").lower()
            if (
                not is_hotfix
                and checklist.get("total", 0) > 0
                and checklist.get("unchecked", 0) > 0
            ):
                blockers.append(
                    f"Expected Tribute has {checklist['unchecked']} unchecked item(s) of "
                    f"{checklist['total']} — complete the checklist or mark cancelled [~] before advancing."
                )

            wt_for_guard = quest.worktree if (quest.worktree and Path(quest.worktree).is_dir()) else None
            base_branch = getattr(args, "base", None) or "castle"
            if wt_for_guard:
                git_stat = git_ops.get_worktree_git_status(Path(wt_for_guard), base=base_branch)
                if git_stat.get("dirty"):
                    blockers.append(
                        "Worktree has uncommitted files — stage and commit deliverables "
                        "(Gate 2) before advancing."
                    )
                behind = git_stat.get("behind")
                if behind is not None and behind > 0:
                    blockers.append(
                        f"{behind} commit(s) behind {base_branch} — run the deferred rebase "
                        f"`git merge {base_branch}` (zero drift) before advancing."
                    )
                actual_branch = (git_stat.get("branch") or "").replace("refs/heads/", "")
                quest_branch = (quest.branch or "").replace("refs/heads/", "")
                if actual_branch and quest_branch and actual_branch != quest_branch:
                    blockers.append(
                        f"Worktree is on branch '{actual_branch}' but frontmatter says "
                        f"'{quest_branch}' — reconcile before advancing."
                    )
            elif not wt_for_guard and quest.kind not in ("epic", "scout"):
                blockers.append(
                    "Quest has no resolvable worktree — a code-bearing quest cannot render "
                    "tribute without one."
                )

            if blockers and not force:
                blocker_lines = "\n".join(f"  🔴 {b}" for b in blockers)
                print(
                    f"ERROR: Cannot advance {quest.id} to TRIBUTE_READY: {len(blockers)} blocker(s).\n"
                    f"{blocker_lines}\n\n"
                    f"To bypass this check, use: python3 -m court.cli advance {quest.id} TRIBUTE_READY --force --note \"<reason>\"",
                    file=sys.stderr,
                )
                sys.exit(1)
            elif blockers:
                forced_note = (
                    f"(FORCED - {len(blockers)} TRIBUTE-READINESS BLOCKER(S): {'; '.join(blockers)}) {args.note}"
                ).strip()
                quest.set_status(new_status, forced_note)
            else:
                quest.set_status(new_status, args.note or "")

        elif new_status == "READY_TO_RAZE" and quest.kind == "epic":
            # Epic Closing Pathway (Q135): an Epic owns no code of its own —
            # all real changes live in its child Quests. Once every child is
            # independently complete and promoted, the Epic can close straight
            # to READY_TO_RAZE (Ready to Raze) without a redundant
            # Gatehouse integration pass on the Epic record itself. Its own
            # Tribute Rendered section is instead an aggregation of every
            # child's rollups (Ballad/Tribute/Tally/Penance/Opinion).
            children = store.get_epic_children(quest.id)
            incomplete = [c for c in children if c.status not in ("READY_TO_RAZE", "DONE")]

            if incomplete and not force:
                incomplete_str = ", ".join(f"{c.id} [{c.status}]" for c in incomplete)
                print(
                    f"ERROR: Cannot advance Epic {quest.id} to READY_TO_RAZE: "
                    f"{len(incomplete)} of {len(children)} child Quest(s) are not yet complete: {incomplete_str}\n\n"
                    f"To bypass this check, use: python3 -m court.cli advance {quest.id} READY_TO_RAZE --force --note \"<reason>\"",
                    file=sys.stderr,
                )
                sys.exit(1)

            manifest = store.rollup_ship_manifest(
                epic=quest.id, status="READY_TO_RAZE,DONE", include_archive=True
            )
            rendered = store.render_ship_manifest_markdown(manifest, epic_id=quest.id)
            quest.set_section("Tribute Rendered", rendered, mode="replace")

            if incomplete:
                forced_note = (
                    f"(FORCED - {len(incomplete)} of {len(children)} CHILD QUEST(S) INCOMPLETE) {args.note}"
                ).strip()
                quest.set_status(new_status, forced_note)
            else:
                note = args.note or (
                    f"Epic closed directly to READY_TO_RAZE: all {len(children)} child Quest(s) "
                    f"complete/promoted; aggregated rollup from children into Tribute Rendered."
                )
                quest.set_status(new_status, note)

        elif new_status == "READY_TO_RAZE":
            # Existing non-epic path, unchanged: git merge-status + clean-worktree
            # checks only make sense for code-bearing Quests/Scouts, not Epic
            # overview records (handled above).
            target = quest.branch or quest.worktree
            merge_res = git_ops.check_merged_status(target)
            is_scout = (quest.kind == "scout" or quest.section == "Investigation")

            # cogship-082 gate (5th falsified-Gatekeeper incident): a Quest
            # leaves GATE only with engine-stamped proof that the unified suite
            # actually ran over the code being promoted. The Gatekeeper's own
            # report is not evidence — its suite runs were dying on the shell
            # tool's 120s timeout while it claimed success.
            if not is_scout and not force:
                head_sha = ""
                if target:
                    sha_res = git_ops._run(["git", "rev-parse", "HEAD"], Path(target)) if Path(target).is_dir() else {"stdout": ""}
                    head_sha = (sha_res.get("stdout") or "").strip()
                suite_check = git_ops.check_suite_proof(
                    cogship_id=quest.cogship_id or "",
                    quest_id=quest.id,
                    head_sha=head_sha,
                )
                if not suite_check.get("valid"):
                    print(
                        f"ERROR: Cannot advance {quest.id} to READY_TO_RAZE: no valid unified-suite proof.\n"
                        f"  {suite_check.get('reason')}\n\n"
                        f"Run the suite via the engine (it stamps durable proof):\n"
                        f"  python3 -m court.cli runsuite {'--cogship ' + quest.cogship_id if quest.cogship_id else quest.id}"
                        f" --dir <gatehouse-or-quest worktree>\n\n"
                        f"To bypass this check, use: python3 -m court.cli advance {quest.id} READY_TO_RAZE --force --note \"<reason>\" "
                        f"(the ledger entry will be marked FORCED)",
                        file=sys.stderr,
                    )
                    sys.exit(1)

            if not merge_res.get("clean_worktree"):
                if not force:
                    print(
                        f"ERROR: Cannot advance {quest.id} to READY_TO_RAZE: worktree has uncommitted files.\n"
                        f"Uncommitted files: {len(merge_res.get('uncommitted_files', []))}\n"
                        f"Recommendation: {merge_res.get('recommendation')}\n\n"
                        f"To bypass this check, use: python3 -m court.cli advance {quest.id} READY_TO_RAZE --force --note \"<reason>\"",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                else:
                    forced_note = f"(FORCED - DIRTY WORKTREE) {args.note}".strip()
                    quest.set_status(new_status, forced_note)
            elif not is_scout and not merge_res.get("is_merged"):
                if not force:
                    print(
                        f"ERROR: Cannot advance {quest.id} to READY_TO_RAZE: branch is not merged into gatehouse/castle.\n"
                        f"Unmerged commits: {merge_res.get('unmerged_commits_count', 0)}, Diff: {merge_res.get('has_diff', False)}\n"
                        f"Recommendation: {merge_res.get('recommendation')}\n\n"
                        f"To bypass this check, use: python3 -m court.cli advance {quest.id} READY_TO_RAZE --force --verified-commit <sha> --note \"<reason>\"",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                # cogship-076/077 truthfulness fix (2026-09-13): the old
                # --verified-commit hatch only proved *some* commit was
                # reachable from castle. cogship-076 passed it with a
                # backwards castle->convoy merge carrying pure bookkeeping;
                # cogship-077 passed it for five quests against one hash while
                # three manifest branches were never merged at all. The claim
                # being forced is "this quest's branch was promoted", so the
                # QUEST BRANCH TIP itself must be an ancestor of the trunk,
                # and the branch must actually carry production content.
                quest_branch = quest.branch or getattr(quest, "tree_branch", "")
                if not quest_branch:
                    print(
                        f"ERROR: --force on an unmerged READY_TO_RAZE requires a resolvable branch "
                        f"(quest.branch is unset; set it with `court set-field {quest.id} branch <branch>`). "
                        f"The branch tip itself — not an arbitrary commit — must be verifiable.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                promo = git_ops.verify_quest_promotion(quest_branch, trunk="castle")
                if not promo.get("ok"):
                    print(
                        f"ERROR: --force refused: promotion claim for branch '{quest_branch}' failed verification.\n"
                        f"  {promo.get('reason')}\n"
                        f"An arbitrary --verified-commit SHA is no longer accepted: only the quest branch tip's "
                        f"own ancestry plus real production content proves promotion.\n\n"
                        f"Recommendation: {merge_res.get('recommendation')}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                forced_note = (
                    f"(FORCED - UNMERGED per merge-status; but branch tip {promo['tip'][:12]} independently "
                    f"verified as an ancestor of castle via merge-base (branch-tip reachability only — this "
                    f"proves the branch's commits are IN castle, not who merged them) with "
                    f"{len(promo['production_files'])} production file(s) present on the branch) {args.note}"
                ).strip()
                quest.set_status(new_status, forced_note)
                quest.cogship_promoted_commit = promo["tip"]
            else:
                quest.set_status(new_status, args.note or "")
        else:
            quest.set_status(new_status, args.note or "")

        store.save(quest, auto_commit=auto_commit, commit_msg=f"court: advance {quest.id} to {new_status}")
        print(f"{quest.id}: {quest.status}")

        # Automated Scout-to-Quest lifecycle transition (Q126): a production
        # Quest chartered from a Scout's report links back via `scout_of`.
        # The moment that successor actually starts real work (WORKING), the
        # source Scout has served its purpose — findings are already captured
        # in its 5-part Scout Report — so it can be queued for teardown
        # without waiting on a human to remember to close it out by hand.
        # (Helper shared with `court dispatch-complete`, Q183.)
        if new_status == "WORKING":
            _auto_transition_source_scout_on_working(quest, auto_commit=auto_commit)


def _auto_transition_source_scout_on_working(quest: Quest, auto_commit: bool = True) -> None:
    """Q126 Scout-to-Quest lifecycle side-effect shared by `court advance <id>
    WORKING` and `court dispatch-complete`: when a successor production Quest
    enters WORKING, auto-transition its `scout_of` source Scout to
    READY_TO_RAZE (non-merging teardown queue). No-op when the Quest has no
    `scout_of` link, the link doesn't resolve to a Scout/Investigation record,
    or the Scout is already closed out."""
    if not getattr(quest, "scout_of", ""):
        return
    scout_id = quest.scout_of
    try:
        scout = store.load(scout_id)
    except Exception as e:
        print(f"⚠️  {quest.id}: scout_of={scout_id} set but could not be loaded ({e}); skipping auto-transition.")
        return
    is_scout_kind = (scout.kind == "scout" or scout.section == "Investigation")
    if not is_scout_kind:
        print(f"⚠️  {quest.id}: scout_of={scout_id} is not a Scout/Investigation record (kind={scout.kind!r}); skipping auto-transition.")
        return
    if scout.status in ("READY_TO_RAZE", "DONE"):
        print(f"ℹ️  {quest.id}: source Scout {scout_id} already [{scout.status}]; nothing to transition.")
        return
    scout.log_ledger(
        scout.status, "READY_TO_RAZE",
        f"Auto-transitioned: successor production Quest {quest.id} entered WORKING "
        f"(scout_of={scout_id}) — this Scout's findings are captured in its Scout Report; "
        f"queued for non-merging teardown into Ashes.",
    )
    scout.status = "READY_TO_RAZE"
    store.save(scout, auto_commit=auto_commit, commit_msg=f"court: auto-transition source Scout {scout_id} to READY_TO_RAZE (successor {quest.id} entered WORKING)")
    print(f"🔭 Auto-transitioned source Scout {scout_id} -> READY_TO_RAZE (successor {quest.id} entered WORKING).")


def cmd_log(args):
    quest = store.load(args.quest_id)
    quest.log_ledger(quest.status, quest.status, args.note)
    auto_commit = not getattr(args, "no_commit", False)
    store.save(quest, auto_commit=auto_commit, commit_msg=f"court: log note on {quest.id}")
    print(f"Logged note on {quest.id}")


def cmd_commute(args):
    """Record a post-deployment commutation as completed on a single Quest.

    Appends one dated bullet to the Quest's Cogship Log:
        - **Commutation (YYYY-MM-DD):** <what was executed>
    The dated Cogship Log entry is the completion marker: once present, the
    Quest drops out of `court status`'s "⚡ COMMUTATIONS REQUIRED" section and
    the /ship Commutation Manifest, moving to the collapsed
    "✅ Commutations Done (N)" line instead. The MoC's original
    **Commutation:** instruction in the audit is left untouched, preserving the
    audit history.
    """
    quest = store.load(args.quest_id)
    # cogship-076 gate: commutation paperwork must not outrun code. The
    # retracted 03:0xZ logs (four quests) recorded production satisfaction for
    # quests whose code never landed. A FORCED promotion marker demands the
    # code-presence audit pass first.
    if _last_ledger_entry_is_forced(quest) and not getattr(args, "force", False):
        promo = _code_presence_check(quest)
        if not promo.get("ok"):
            print(
                f"ERROR: {quest.id} carries a FORCED promotion marker and its code-presence audit failed:\n"
                f"  {promo.get('reason')}\n"
                f"Refusing to record commutation satisfaction for code that never landed.\n"
                f"Re-verify with `court verify-manifest` or pass --force to override explicitly.",
                file=sys.stderr,
            )
            sys.exit(1)
    if args.file:
        note = Path(args.file).read_text(encoding="utf-8")
    else:
        note = args.note or ""
    if not note.strip():
        print("ERROR: provide completion details via --note <text> or --file <path>", file=sys.stderr)
        sys.exit(1)
    if quest.commutation_log_entries() and not getattr(args, "force", False):
        print(f"ℹ️  {quest.id} already has a commutation completion entry in its Cogship Log; nothing appended (use --force to log another).")
        return
    if not quest.commutation_required():
        print(f"⚠️  {quest.id} has no recorded commutation instruction (Master of Coin marked it none/n/a); appending an audit-trail entry anyway.")
    entry = quest.append_commutation(note)
    auto_commit = not getattr(args, "no_commit", False)
    store.save(quest, auto_commit=auto_commit, commit_msg=f"court: commute {quest.id} — commutation logged as completed")
    print(f"✅ Logged commutation completion on {quest.id} (Cogship Log):\n   {entry}")


def cmd_set_field(args):
    quest = store.load(args.quest_id)
    if not hasattr(quest, args.field):
        print(f"ERROR: unknown field {args.field!r}", file=sys.stderr)
        sys.exit(1)
    if args.field in DEDICATED_COMMAND_FIELDS:
        print(f"ERROR: {DEDICATED_COMMAND_FIELDS[args.field]}", file=sys.stderr)
        sys.exit(1)
    if args.field == "branch":
        is_valid, err = validate_branch_name(args.value)
        if not is_valid:
            print(f"ERROR: {err}", file=sys.stderr)
            sys.exit(1)
    setattr(quest, args.field, args.value)
    quest.updated_at = now_iso()
    auto_commit = not getattr(args, "no_commit", False)
    store.save(quest, auto_commit=auto_commit, commit_msg=f"court: set {args.field} on {quest.id}")
    print(f"{quest.id}.{args.field} = {args.value}")


def cmd_model(args):
    """Print the canonical model ID for a role, resolved from the manifest
    (.court/config.json models map) via the config loader (Q455)."""
    role = args.role.replace("-", "_")
    if role not in config.KNOWN_ROLE_MODELS:
        print(f"Unknown role: {args.role} (known: {', '.join(config.KNOWN_ROLE_MODELS)})")
        return 2
    print(canonical_model_id(config.get_model(role), provider=config.get_provider(role)))
    return 0


def cmd_set_section(args):
    quest = store.load(args.quest_id)
    if args.file:
        content = Path(args.file).read_text(encoding="utf-8")
    else:
        content = args.content or ""
    quest.set_section(args.section, content, mode=("append" if args.append else "replace"))
    auto_commit = not getattr(args, "no_commit", False)
    store.save(quest, auto_commit=auto_commit, commit_msg=f"court: update {args.section} on {quest.id}")
    print(f"Updated section {args.section!r} on {quest.id}")


def _print_charter_next_steps(quest: Quest, am_section: str) -> None:
    """Print the copy-pasteable NEXT STEPS block after `court charter`."""
    branch = quest.branch or quest.tree_branch
    section_display = am_section or "(unset — pick Bug fix/Feature/Optimization)"
    wt_name = branch.replace("/", "-")
    _print_next_steps(
        f"🚀 NEXT STEPS — DISPATCH THE SERF FOR {quest.id}",
        [
            f"1. Stand up the Serf worktree and session directly via Court CLI standup:",
            f"",
            f"   python3 -m court.cli dispatch {quest.id} --standup",
            f"",
            f"   (Initializes worktree under .kilo/worktrees/, configures default_agent: serf,",
            f"   enforces task: deny permissions, and passes pure charter task instructions.",
            f"   Zero persona prompt injection; zero prompt corruption.)",
            f"",
            f"   Alternatively, via Kilo CLI directly:",
            f'   kilo --worktree {wt_name} --agent serf --model "{config.get_model("serf")}" --provider "{config.get_provider("serf")}"',
            f'   or:',
            f'   kilo worktree create {wt_name}',
            f'   kilo run --agent serf --model "{config.get_model("serf")}" --provider "{config.get_provider("serf")}" --dir <WORKTREE_PATH> "<TASK_PROMPT>"',
            f"",
            f"2. Or if manually spawning a session in any UI in section \"{section_display}\":",
            f"   python3 -m court.cli dispatch-complete {quest.id} \\",
            f"       --session-id <SESSION_ID> --branch {branch} --worktree <WORKTREE_PATH>",
        ],
    )


# In-flight statuses whose branches may carry unlanded migrations (advisory population).
_MIGRATION_LANE_IN_FLIGHT = (
    "OPEN", "PLANNED", "CHARTERED", "DISPATCHED", "QUESTING", "WORKING", "TRIBUTE_READY", "GATE",
)


def _print_migration_lane_advisory(chartered_quest: Quest, base_ref: str = "castle") -> None:
    """Charter-time collision advisory (2026-09-15 migration-fork class).

    N parallel Quests branching from the same castle tip each independently
    autogenerate the same next migration number - a pigeonhole problem, not a
    model/judgment problem, invisible to git (distinct filenames merge clean)
    and to per-Quest suites (each sees only its own single leaf). Surface the
    in-flight migration-lane population mechanically at charter time so the
    Steward sequences lanes or consciously accepts the convoy merge path.
    Advisory only - the deterministic refusal gate lives in `court collect`.
    """
    try:
        root = git_ops.get_repo_root()
    except Exception:
        return
    branches: list[tuple[str, str]] = []
    for q in store.list_all():
        if q.id == chartered_quest.id or not q.branch:
            continue
        if q.status in _MIGRATION_LANE_IN_FLIGHT:
            branches.append((q.id, q.branch))
    if not branches:
        return
    try:
        activity = migration_guard.migration_activity(root, branches, base_ref)
    except Exception:
        return
    if not activity:
        return
    print("\n🗺️  Migration-lane advisory — in-flight Quests adding migrations to shared app namespaces:")
    for app in sorted(activity):
        entries = sorted({e["quest_id"] for e in activity[app]})
        print(f"   - apps/{app}/migrations/: {', '.join(entries)}")
    print(
        "   Sequence these lanes or consciously accept the merge path "
        "(Gatekeeper template → Migration Graph Doctrine); most of this "
        "collision class dies at dispatch time."
    )


def cmd_charter(args):
    """Composite charter (Q183): fold M'Lord's notes into `The Kingdom Requires`,
    idempotently advance OPEN -> PLANNED, compute the canonical branch when
    unset, and print the NEXT STEPS block for the Kilo CLI
    Serf spawn. One command, ONE commit — replaces the old 2-invocation
    pre-dispatch plumbing sequence (set-section --append + advance PLANNED).

    Q185: also accepts `--pillory-of <predecessor_id>` to atomically link a
    successor Quest to the PUNISHED predecessor it was chartered from —
    the dedicated replacement for the raw `pillory_of`/`pilloried_by`
    set-field pair `cmd_pillory` used to tell callers to run by hand.
    """
    quest = store.load(args.quest_id)
    auto_commit = not getattr(args, "no_commit", False)
    changed: list[str] = []

    # 0. Pillory successor linkage (Q185): link both sides in one commit.
    #    Do this first so a bad predecessor id fails loudly before any other
    #    mutation on this Quest is made.
    if getattr(args, "pillory_of", None):
        predecessor_id = args.pillory_of
        try:
            predecessor = store.load(predecessor_id)
        except Exception as e:
            print(f"ERROR: --pillory-of {predecessor_id!r}: {e}", file=sys.stderr)
            sys.exit(1)
        if predecessor.status != "PUNISHED":
            print(
                f"ERROR: --pillory-of target {predecessor_id} is not PUNISHED "
                f"(currently [{predecessor.status}]) — only a pilloried Quest has "
                f"Decrees for a successor to inherit.",
                file=sys.stderr,
            )
            sys.exit(1)
        quest.pillory_of = predecessor.id
        predecessor.pilloried_by = quest.id
        store.save(predecessor, auto_commit=auto_commit, commit_msg=f"court: link {predecessor.id} pilloried_by {quest.id}")
        changed.append(f"pillory_of={predecessor.id}")

    # 1. Fold M'Lord's notes via the same mechanism `cmd_set_section --append`
    #    uses — the underlying Quest method, called directly (never a shell-out).
    if getattr(args, "notes", None):
        quest.set_section("The Kingdom Requires", f"M'Lord's Charter Notes: {args.notes}", mode="append")
        changed.append("notes appended")

    # 2. Optional Agent Manager section-lane override. Tags follow when they
    #    merely mirrored the old section (mirrors cmd_new's tags default).
    if getattr(args, "section", None):
        if not quest.tags or quest.tags == quest.section:
            quest.tags = args.section
        quest.section = args.section
        changed.append(f"section={args.section}")

    # 3. Idempotent advance to PLANNED (reuse quest.set_status, same as
    #    cmd_advance). PLANNED-or-later is a no-op; side-states are untouched.
    if quest.status == "OPEN":
        quest.set_status("PLANNED", "Chartered via composite `court charter`")
        changed.append("OPEN → PLANNED")
    elif quest.status in _CHARTER_PIPELINE_ORDER:
        print(f"ℹ️  {quest.id} is already [{quest.status}] ({status_label(quest.status)}) — advance to PLANNED skipped (charter is idempotent).")
    else:
        print(f"⚠️  {quest.id} is in side-state [{quest.status}] ({status_label(quest.status)}) — status untouched (resolve the side-state first).")

    # 4. Canonical branch via quest.tree_branch (same property cmd_new uses)
    #    unless one is already set.
    if not quest.branch:
        branch_candidate = quest.tree_branch
        is_valid, err = validate_branch_name(branch_candidate)
        if not is_valid:
            print(f"ERROR: {err}", file=sys.stderr)
            sys.exit(1)
        quest.branch = branch_candidate
        changed.append(f"branch={branch_candidate}")

    # 5. ONE save -> ONE commit for the whole composite action (cmd_new's
    #    bundling pattern: mutate the Quest object fully, then save once).
    store.save(quest, auto_commit=auto_commit, commit_msg=f"court: charter {quest.id}")
    summary = f"; ".join(changed) if changed else "no changes"
    print(f"Chartered {quest.id}: status=[{quest.status}] branch={quest.branch or '-'} section={quest.section or '-'} ({summary})")

    _print_migration_lane_advisory(quest)

    if getattr(args, "dispatch", False):
        print(f"\n🚀 --dispatch specified: proceeding directly to Serf standup...")
        cmd_dispatch(args)
    else:
        _print_charter_next_steps(quest, am_section=getattr(args, "section", None) or quest.section)


def _print_dispatch_next_steps(quest: Quest, standup_res: Optional[dict] = None) -> None:
    """Q183: print the NEXT STEPS reminder after `court dispatch-complete`."""
    is_scout = getattr(quest, "kind", "") == "scout" or getattr(quest, "section", "") == "Investigation"
    role_name = "Scout" if is_scout else "Serf"
    role_mode = "scout" if is_scout else "serf"
    steps = [
        f"- {role_name} session {quest.serf_session_id or '-'} ({quest.serf_model or '-'}) is toiling in",
        f"  {quest.worktree or '-'} on branch {quest.branch or '-'}.",
        f"- Initialized under agent mode '{role_mode}' with pure charter task instructions via Kilo CLI.",
        f"- Nothing to do right now: this is normal {role_name} toil time.",
    ]
    if standup_res and standup_res.get("log"):
        steps.append(f"- Worker log: tail -f {standup_res['log']}")
    elif quest.worktree:
        steps.append(f"- Worker log: tail -f {quest.worktree}/.kilo/{role_mode}.log")
    steps.append(f"- Once the {role_name} reports done, collect the tribute: `court levy {quest.id}`")
    _print_next_steps(f"⚙️ NEXT STEPS — {quest.id} IS NOW WORKING", steps)


def cmd_dispatch(args):
    """Dispatch a Quest to a worktree (creates worktree and configures agent mode,
    advances to WORKING)."""
    quest = store.load(args.quest_id)
    auto_commit = not getattr(args, "no_commit", False)

    branch = getattr(args, "branch", None) or quest.branch or quest.tree_branch
    is_valid, err = validate_branch_name(branch)
    if not is_valid:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    worktree = getattr(args, "worktree", None)
    create_wt = getattr(args, "create_worktree", False) or getattr(args, "native", False)
    standup = getattr(args, "standup", False)
    no_run = getattr(args, "no_run", False)
    run_now = not no_run
    serf_model = getattr(args, "serf_model", None) or config.get_model("serf")

    kilo_bin = find_kilo_binary()
    root = git_ops.get_repo_root()
    base_branch = getattr(args, "base", "castle") or "castle"

    # If neither worktree nor create_wt was passed, default to automated standup
    if not worktree and not create_wt:
        standup = True

    if not worktree:
        if kilo_bin and not getattr(args, "native", False) and not getattr(args, "create_worktree", False):
            wt_name = branch.replace("/", "-")
            worktree_path = root / ".kilo" / "worktrees" / wt_name
            if not worktree_path.is_dir():
                print(f"🌲 Creating Kilo worktree '{wt_name}' on branch {branch}...")
                try:
                    subprocess.run(
                        [str(kilo_bin), "worktree", "create", wt_name],
                        cwd=str(root),
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=300,
                    )
                    git_ops.clear_git_cache()
                except subprocess.TimeoutExpired:
                    print(f"ERROR: 'kilo worktree create {wt_name}' timed out after 300s.", file=sys.stderr)
                    sys.exit(1)
                except (subprocess.CalledProcessError, FileNotFoundError):
                    wt_res = git_ops.create_git_worktree(
                        str(worktree_path),
                        branch=branch,
                        base_branch=base_branch,
                    )
                    if not wt_res["ok"]:
                        print(f"ERROR: Failed to create git worktree: {wt_res.get('output')}", file=sys.stderr)
                        sys.exit(1)
            worktree = str(worktree_path)
            subprocess.run(["git", "-C", worktree, "branch", "-m", branch], capture_output=True, timeout=60)
            subprocess.run(["git", "-C", worktree, "merge", base_branch, "--ff-only"], capture_output=True, timeout=120)
            print(f"✅ Worktree ready: {worktree}")
        else:
            short_id = quest.id.split("-")[0].lower()
            worktree_path = root / ".court" / "worktrees" / short_id
            print(f"🌲 Creating native git worktree at {worktree_path} on branch {branch}...")
            wt_res = git_ops.create_git_worktree(
                str(worktree_path),
                branch=branch,
                base_branch=base_branch,
            )
            if not wt_res["ok"]:
                print(f"ERROR: Failed to create git worktree: {wt_res.get('output')}", file=sys.stderr)
                sys.exit(1)
            worktree = wt_res.get("path") or str(worktree_path)
            print(f"✅ Git worktree ready: {worktree}")

    worktree_path = Path(worktree).resolve()

    # Determine agent role (serf vs scout)
    is_scout = (getattr(quest, "kind", "") == "scout" or getattr(quest, "section", "") == "Investigation")
    agent_role = getattr(args, "agent", None) or ("scout" if is_scout else "serf")
    role_label = "Scout" if agent_role == "scout" else "Serf"

    # Configure worktree-scoped default_agent
    setup_worktree_agent_config(worktree_path, agent_role)

    # Run setup-script if present
    setup_script = root / ".kilo" / "setup-script"
    if setup_script.is_file() and os.access(setup_script, os.X_OK):
        subprocess.run(
            [str(setup_script)],
            env={**os.environ, "WORKTREE_PATH": str(worktree_path), "REPO_PATH": str(root)},
            cwd=str(worktree_path),
            capture_output=True,
        )

    # Build pure task prompt (zero persona boilerplate)
    task_prompt = getattr(args, "prompt", None) or build_serf_task_prompt(quest)

    # Stand up session if requested or available
    session_id = getattr(args, "session_id", None)
    standup_res = standup_kilo_session(
        worktree_path=worktree_path,
        agent=agent_role,
        model=serf_model,
        prompt=task_prompt,
        title=f"{quest.id} {role_label} Worker",
        kilo_bin=kilo_bin,
        run_now=run_now,
        watch_quest=quest.id,
    )
    if not session_id:
        if getattr(args, "create_worktree", False) or getattr(args, "native", False):
            session_id = "native"
        else:
            session_id = standup_res.get("session_id") or f"kilo-{agent_role}"

    quest.branch = branch
    quest.worktree = str(worktree_path)
    quest.serf_session_id = session_id
    quest.serf_model = serf_model
    quest.updated_at = now_iso()

    entered_working = False
    if quest.status in ("OPEN", "PLANNED", "CHARTERED"):
        # Q695 (2026-09-28): CHARTERED quests were skipped here ("already
        # [CHARTERED] — transitions skipped"), leaving a freshly dispatched
        # serf toiling under a stale status. CHARTERED is dispatchable like
        # PLANNED — route it through the same DISPATCHED -> WORKING path.
        quest.set_status("DISPATCHED", f"{role_label} dispatched (`court dispatch`)")
        quest.set_status("WORKING", f"{role_label} toiling in worktree (`court dispatch`)")
        entered_working = True
    elif quest.status == "DISPATCHED":
        quest.set_status("WORKING", f"{role_label} toiling in worktree (`court dispatch`)")
        entered_working = True
    else:
        print(f"ℹ️  {quest.id} is already [{quest.status}] ({status_label(quest.status)}) — DISPATCHED/WORKING transitions skipped.")

    store.save(quest, auto_commit=auto_commit, commit_msg=f"court: dispatch {quest.id}")
    print(f"Dispatched {quest.id}: branch={quest.branch} worktree={quest.worktree} serf={quest.serf_session_id} model={quest.serf_model} status=[{quest.status}]")

    if entered_working:
        _auto_transition_source_scout_on_working(quest, auto_commit=auto_commit)

    _print_dispatch_next_steps(quest, standup_res=standup_res)


def cmd_dispatch_complete(args):
    """Composite post-dispatch bookkeeping (Q183). Alias for cmd_dispatch."""
    return cmd_dispatch(args)


def cmd_patchwork(args):
    """Fast-track hotfix pipeline: one-shot intake, serf dispatch, inline test verification, and promotion."""
    app = getattr(args, "app", None)
    concern = getattr(args, "concern", None)
    title = getattr(args, "title", None)
    quest_id = getattr(args, "quest_id", None)
    auto_commit = not getattr(args, "no_commit", False)
    base_branch = getattr(args, "base", "castle") or "castle"

    # 1. Intake & One-shot Creation + Dispatch
    if app and concern and title:
        qid = store.make_id(app, concern)
        quest = Quest(
            id=qid,
            title=title,
            kind="quest",
            app=app,
            concern=concern,
            section="Bug fix",
            tags=getattr(args, "tags", "") or "Bug fix,hotfix",
            status="OPEN",
        )
        branch_candidate = getattr(args, "branch", None) or quest.tree_branch
        is_valid, err = validate_branch_name(branch_candidate)
        if not is_valid:
            print(f"ERROR: {err}", file=sys.stderr)
            sys.exit(1)
        quest.branch = branch_candidate

        goal = getattr(args, "goal", None) or f"Hotfix: {title}.\n\nApply targeted code fix for {concern} in {app}."
        quest.set_section("The Kingdom Requires", goal)

        test_cmd = getattr(args, "test_cmd", None) or "pytest"
        self_advance_reminder = (
            f"- [ ] **Self-advance (do this LAST)**: once Tribute is rendered and "
            f"`git rev-list --count HEAD..castle` is 0, run "
            f"`python3 -m court.cli advance {qid} TRIBUTE_READY` yourself. "
            f"Nothing else flips the status out of WORKING."
        )
        checklist = [
            f"- [ ] Implement minimal code fix for {title} (<= 100 lines diff, <= 3 files)",
            f"- [ ] Verify clean test run: `{test_cmd}`",
            f"- [ ] Verify zero roleplay leakage in production code, comments, and commit messages",
            f"- [ ] Merge castle tip and ensure zero drift (behind: 0)",
            self_advance_reminder,
        ]
        quest.set_section("Expected Tribute", "\n".join(checklist))
        quest.log_ledger("-", quest.status, "Hotfix quest created via court patchwork")
        store.save(quest, auto_commit=auto_commit, commit_msg=f"court: create {quest.id}")

        quest.set_status("PLANNED", "Hotfix auto-chartered via court patchwork")
        store.save(quest, auto_commit=auto_commit, commit_msg=f"court: charter {quest.id}")

        print(f"⚡ Hotfix {quest.id} chartered: branch={quest.branch} section={quest.section}")
        print("🚀 Dispatching focused Serf worker to worktree...")

        setattr(args, "quest_id", quest.id)
        setattr(args, "standup", True)
        setattr(args, "section", "Bug fix")
        setattr(args, "tags", quest.tags)
        setattr(args, "branch", quest.branch)
        cmd_dispatch(args)
        return

    if not quest_id:
        print(
            "ERROR: Provide either (--app, --concern, --title) to create a hotfix, "
            "or specify a quest_id to inspect, verify, collect, or promote an existing hotfix.",
            file=sys.stderr,
        )
        sys.exit(1)

    quest = store.load(quest_id)

    # 2. In-tree Verification
    if getattr(args, "verify", False):
        wt = quest.worktree or (git_ops.get_repo_root() / ".kilo" / "worktrees" / quest.branch.replace("/", "-"))
        if not Path(wt).is_dir():
            wt_cand = git_ops.find_worktree_for_quest(quest)
            if wt_cand and Path(wt_cand).is_dir():
                wt = wt_cand
            else:
                print(f"ERROR: worktree not found at {wt}", file=sys.stderr)
                sys.exit(1)
        test_cmd = getattr(args, "test_cmd", None)
        print(f"🔍 Running hotfix verification for {quest.id} in {wt}...")
        if test_cmd:
            res = git_ops.run_test_command(str(wt), test_cmd)
            if res.get("ok"):
                print(f"✅ In-tree verification PASSED:\n{res.get('stdout', '')}")
            else:
                print(f"❌ In-tree verification FAILED:\n{res.get('stderr') or res.get('stdout')}", file=sys.stderr)
                sys.exit(1)
        else:
            suite_res = git_ops.run_unified_suite(str(wt), quest_id=quest.id)
            if suite_res.get("ok"):
                print(f"✅ In-tree suite PASSED at {suite_res.get('head_sha', '')[:12]}")
            else:
                print(f"❌ In-tree suite FAILED: {suite_res.get('error')}", file=sys.stderr)
                sys.exit(1)
        return

    # 3. Inline Collect & Gatehouse Pack
    if getattr(args, "collect", False):
        if quest.status == "WORKING":
            tribute = quest.body_sections.get("Tribute Rendered", "").strip()
            if tribute:
                quest.set_status("TRIBUTE_READY", "Hotfix auto-advanced to TRIBUTE_READY")
                store.save(quest, auto_commit=auto_commit, commit_msg=f"court: advance {quest.id} to TRIBUTE_READY")

        if not quest.body_sections.get("Master of Coin's Audit", "").strip():
            stat_text = ""
            if quest.worktree and Path(quest.worktree).is_dir():
                diff_info = git_ops.get_worktree_diff(quest.worktree, base=base_branch, stat_only=True)
                stat_text = diff_info.get("diff", "").strip()
            diff_block = f"\n- **Diff Footprint**:\n```\n{stat_text}\n```\n" if stat_text else "\n"
            audit_text = (
                "### Master of Coin Audit (Hotfix Fast-Track)\n"
                "- **Verdict**: PASS (Hotfix inline verified)\n"
                "- **Commutation**: None required / Deploy immediately\n"
                "- **UI Review**: None required"
                f"{diff_block}"
            )
            quest.set_section("Master of Coin's Audit", audit_text)
            store.save(quest, auto_commit=auto_commit, commit_msg=f"court: inline hotfix audit {quest.id}")
            print(f"🪙 Inline Master of Coin audit recorded for {quest.id}.")

        if quest.status != "GATE":
            cogship_id = store.stamp_cogship([quest], cogship_id=getattr(args, "cogship", None), auto_commit=auto_commit, force=getattr(args, "force_stamp", False))
            quest.set_status("GATE", f"Hotfix packed in {cogship_id} for solo gatehouse promotion")
            store.save(quest, auto_commit=auto_commit, commit_msg=f"court: advance {quest.id} to GATE in {cogship_id}")
            print(f"🛡️  Advanced {quest.id} to GATE in {cogship_id}.")

        print(f"✅ Hotfix {quest.id} is at GATE and ready for promotion.")
        print(f"   To promote immediately: python3 -m court.cli patchwork {quest.id} --promote")
        return

    # 4. Inline Promote to castle
    if getattr(args, "promote", False):
        wt = quest.worktree or (git_ops.get_repo_root() / ".kilo" / "worktrees" / quest.branch.replace("/", "-"))
        if not wt or not Path(wt).is_dir():
            wt_cand = git_ops.find_worktree_for_quest(quest)
            if wt_cand and Path(wt_cand).is_dir():
                wt = wt_cand
            else:
                print(f"ERROR: worktree not found for {quest.id} at {wt}", file=sys.stderr)
                sys.exit(1)

        print(f"🧪 Running gate verification suite for hotfix {quest.id}...")
        test_cmd = getattr(args, "test_cmd", None)
        if test_cmd:
            res = git_ops.run_test_command(str(wt), test_cmd)
            if not res.get("ok"):
                print(f"❌ Test verification failed:\n{res.get('stderr') or res.get('stdout')}", file=sys.stderr)
                sys.exit(1)
        else:
            suite_res = git_ops.run_unified_suite(str(wt), quest_id=quest.id)
            if not suite_res.get("ok"):
                print(f"❌ Integration suite failed: {suite_res.get('error')}", file=sys.stderr)
                sys.exit(1)

        # Merge branch into base_branch (e.g. castle)
        root = git_ops.get_repo_root()
        branch = quest.branch
        print(f"🏰 Promoting hotfix {quest.id} ({branch}) into {base_branch}...")
        merge_res = subprocess.run(["git", "-C", str(root), "merge", branch, "--ff-only"], capture_output=True, text=True)
        if merge_res.returncode != 0:
            merge_res = subprocess.run(["git", "-C", str(root), "merge", branch, "-m", f"court: promote hotfix {quest.id} ({quest.title})"], capture_output=True, text=True)
            if merge_res.returncode != 0:
                print(f"❌ Failed to merge {branch} into {base_branch}:\n{merge_res.stderr}", file=sys.stderr)
                sys.exit(1)

        if not quest.cogship_id:
            cogship_id = store.stamp_cogship([quest], cogship_id=getattr(args, "cogship", None), auto_commit=auto_commit, force=getattr(args, "force_stamp", False))
        if not quest.body_sections.get("Master of Coin's Audit", "").strip():
            quest.set_section("Master of Coin's Audit", "### Master of Coin Audit (Hotfix Fast-Track)\n- **Verdict**: PASS (Hotfix inline verified)\n- **Commutation**: None required\n- **UI Review**: None required\n")
        quest.set_status("READY_TO_RAZE", f"Hotfix promoted directly into {base_branch}")
        store.save(quest, auto_commit=auto_commit, commit_msg=f"court: advance {quest.id} to READY_TO_RAZE")
        print(f"🪦 Hotfix {quest.id} successfully promoted to {base_branch} and marked READY_TO_RAZE.")
        print("📦 Next steps:")
        print("   1. View deployment manifest: python3 -m court.cli ship")
        print(f"   2. Teardown worktree:       python3 -m court.cli raze {quest.id}")
        return

    # Default show hotfix status
    print(f"⚡ Hotfix Quest: {quest.id} — {quest.title}")
    print(f"   Status: [{quest.status}] ({status_label(quest.status)})")
    print(f"   Branch: {quest.branch or '-'}")
    print(f"   Worktree: {quest.worktree or '-'}")
    print(f"   Tags: {quest.tags or '-'}")
    if quest.worktree and Path(quest.worktree).is_dir():
        diff_info = git_ops.get_worktree_diff(quest.worktree, base=base_branch, stat_only=True)
        print(f"   Diffstat vs {base_branch}:\n{diff_info.get('diff', 'none')}")
    print("\nAvailable fast-track actions:")
    print(f"   Verify in-tree  : python3 -m court.cli patchwork {quest.id} --verify")
    print(f"   Collect & Gate  : python3 -m court.cli patchwork {quest.id} --collect")
    print(f"   Promote to trunk: python3 -m court.cli patchwork {quest.id} --promote")


def cmd_coin(args):
    """Dispatch Master of Coin into a Quest's worktree via Kilo CLI to audit Tribute."""
    quest = store.load(args.quest_id)
    if not quest.worktree:
        print(f"ERROR: {quest.id} has no worktree path configured", file=sys.stderr)
        sys.exit(1)
    wt = Path(quest.worktree).resolve()
    if not wt.is_dir():
        print(f"ERROR: Worktree directory does not exist: {wt}", file=sys.stderr)
        sys.exit(1)

    if quest.status != "TRIBUTE_READY" and not getattr(args, "force", False):
        print(f"⚠️  {quest.id} is [{quest.status}], not [TRIBUTE_READY]. Pass --force to audit anyway.", file=sys.stderr)
        sys.exit(1)

    kilo_bin = find_kilo_binary()
    if not kilo_bin:
        print("ERROR: Kilo binary not found. Cannot dispatch Master of Coin via CLI.", file=sys.stderr)
        sys.exit(1)

    model = getattr(args, "model", None) or config.get_model("master_of_coin")
    qual_model = canonical_model_id(model)

    tmpl_path = config.find_court_dir() / "templates" / "master_of_coin_review_prompt.md"
    if not tmpl_path.is_file():
        tmpl_path = Path(__file__).resolve().parent.parent / "templates" / "master_of_coin_review_prompt.md"

    if tmpl_path.is_file():
        tmpl_text = tmpl_path.read_text(encoding="utf-8")
    else:
        tmpl_text = "You are the Master of Coin for {{ quest_id }} ({{ quest_title }}). Audit worktree {{ worktree }}."

    rendered_prompt = (
        tmpl_text
        .replace("{{ quest_id }}", quest.id)
        .replace("{{ quest_title }}", quest.title)
        .replace("{{ worktree }}", str(wt))
        .replace("{{ branch }}", quest.branch or "-")
        .replace("{{ epic_id }}", quest.parent_epic or "-")
        .replace("{{ epic_title }}", quest.parent_epic or "-")
    )

    setup_worktree_agent_config(wt, "master_of_coin")

    task_file = wt / ".kilo" / "TASK_COIN.md"
    try:
        task_file.write_text(rendered_prompt, encoding="utf-8")
    except Exception:
        pass

    log_dir = wt / ".kilo"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "coin.log"

    title = f"{quest.id} Master of Coin Audit"
    cmd = [
        str(kilo_bin),
        "run",
        "--agent", "master_of_coin",
        "--model", qual_model,
        "--dir", str(wt),
        "--title", title,
        "--auto",
        rendered_prompt,
    ]

    auto_commit = not getattr(args, "no_commit", False)
    wait = getattr(args, "wait", False)

    if wait:
        print(f"🪙 Running Master of Coin audit for {quest.id} synchronously (agent: master_of_coin, model: {qual_model})...")
        pre_existing = query_kilo_session_ids(wt)
        res = subprocess.run(cmd, cwd=str(wt), env=_engine_spawn_env(wt))
        session_id = query_latest_kilo_session_id(wt, exclude_ids=pre_existing) or query_latest_kilo_session_id(wt)
        if session_id:
            quest.master_of_coin_session_id = session_id
            quest.master_of_coin_model = model
            store.save(quest, auto_commit=auto_commit, commit_msg=f"court: record MoC session {session_id} for {quest.id}")
        # Q699 class: kilo-coin-36177 died with an empty audit on both copies
        # while the dispatch printed success. A completed run that produced no
        # verdict is a failed audit, not a success.
        fresh = store.load(quest.id)
        audit_body = fresh.body_sections.get("Master of Coin's Audit", "").strip()
        if res.returncode == 0 and (not audit_body or ward.is_placeholder(audit_body)):
            print(
                f"ERROR: Master of Coin run exited 0 but produced no verdict — "
                f"'Master of Coin's Audit' is empty on {quest.id}. Re-dispatch "
                f"(`court coin {quest.id}`) and check the log: tail -40 {log_file}",
                file=sys.stderr,
            )
            return 1
        return res.returncode
    else:
        pre_existing = query_kilo_session_ids(wt)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\n--- Master of Coin Audit: {title} ({datetime.now().isoformat()}) ---\n")
        log_out = open(log_file, "a", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(wt),
                stdout=log_out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=_engine_spawn_env(wt),
            )
        finally:
            log_out.close()
        _spawn_watchdog(
            wt, "master_of_coin", proc.pid, "",
            model=qual_model,
            quest_ids=quest.id,
            task_file="TASK_COIN.md",
            exclude_ids=pre_existing,
        )
        session_id = query_latest_kilo_session_id(wt, timeout_seconds=2.5, exclude_ids=pre_existing)
        # kilo-coin-36177 class (Q699): verify the detached process actually
        # survived spawn before recording a session id. A PID-fallback id for
        # an instantly-dead process is a session that never existed — the
        # empty '## Master of Coin's Audit' both copies showed. A MagicMock
        # poll() (tests) is treated as alive.
        poll_res = proc.poll() if hasattr(proc, "poll") else None
        if session_id is None and isinstance(poll_res, int):
            print(
                f"ERROR: Master of Coin process exited immediately (exit {poll_res}); no session was created. "
                f"Log tail: tail -20 {log_file}",
                file=sys.stderr,
            )
            sys.exit(1)
        session_id = session_id or f"kilo-coin-{proc.pid}"
        quest.master_of_coin_session_id = session_id
        quest.master_of_coin_model = model
        store.save(quest, auto_commit=auto_commit, commit_msg=f"court: dispatch MoC {session_id} for {quest.id}")
        print(f"🪙 Dispatched Master of Coin for {quest.id}:")
        print(f"   Session:  {session_id} (PID {proc.pid})")
        print(f"   Agent:    master_of_coin")
        print(f"   Model:    {qual_model}")
        print(f"   Worktree: {wt}")
        print(f"   Log:      tail -f {log_file}")


def cmd_goad(args):
    """Goad an active or stalled Serf session in a worktree via Kilo CLI."""
    quest = store.load(args.quest_id)
    if not quest.worktree:
        print(f"ERROR: {quest.id} has no worktree path configured", file=sys.stderr)
        sys.exit(1)
    wt = Path(quest.worktree).resolve()
    if not wt.is_dir():
        print(f"ERROR: Worktree directory does not exist: {wt}", file=sys.stderr)
        sys.exit(1)

    kilo_bin = find_kilo_binary()
    if not kilo_bin:
        print("ERROR: Kilo binary not found. Cannot goad session via CLI.", file=sys.stderr)
        sys.exit(1)

    is_scout = getattr(quest, "kind", "") == "scout" or getattr(quest, "section", "") == "Investigation"
    agent_role = "scout" if is_scout else "serf"
    role_label = "Scout" if is_scout else "Serf"
    model = getattr(args, "model", None) or quest.serf_model or config.get_model("serf")
    qual_model = canonical_model_id(model)

    tmpl_path = config.find_court_dir() / "templates" / "goad_prompt.md"
    if not tmpl_path.is_file():
        tmpl_path = Path(__file__).resolve().parent.parent / "templates" / "goad_prompt.md"

    if tmpl_path.is_file():
        tmpl_text = tmpl_path.read_text(encoding="utf-8")
    else:
        tmpl_text = "You are being goaded on Quest <QUEST_ID> (branch <canonical_branch>). Please resume work and complete the checklist."

    rendered_prompt = (
        tmpl_text
        .replace("<QUEST_ID>", quest.id)
        .replace("<canonical_branch>", quest.branch or "-")
        .replace("{{ quest_id }}", quest.id)
        .replace("{{ branch }}", quest.branch or "-")
    )

    setup_worktree_agent_config(wt, agent_role)

    task_file = wt / ".kilo" / "TASK_GOAD.md"
    try:
        task_file.write_text(rendered_prompt, encoding="utf-8")
    except Exception:
        pass

    log_dir = wt / ".kilo"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{agent_role}.log"

    title = f"{quest.id} {role_label} Goad"
    cmd = [
        str(kilo_bin),
        "run",
        "--agent", agent_role,
        "--model", qual_model,
        "--dir", str(wt),
        "--title", title,
        "--auto",
        rendered_prompt,
    ]

    auto_commit = not getattr(args, "no_commit", False)
    pre_existing = query_kilo_session_ids(wt)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"\n--- Goad {role_label}: {title} ({datetime.now().isoformat()}) ---\n")
    log_out = open(log_file, "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(wt),
            stdout=log_out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=_engine_spawn_env(wt),
        )
    finally:
        log_out.close()
    _spawn_watchdog(
        wt, agent_role, proc.pid, "",
        model=qual_model,
        quest_ids=quest.id,
        task_file="TASK_GOAD.md",
        exclude_ids=pre_existing,
    )
    session_id = query_latest_kilo_session_id(wt, timeout_seconds=2.5, exclude_ids=pre_existing)
    # Same instant-death verification as cmd_coin (Q699 class): refuse to
    # record a session id for a process that died on spawn.
    poll_res = proc.poll() if hasattr(proc, "poll") else None
    if session_id is None and isinstance(poll_res, int):
        print(
            f"ERROR: Goaded {role_label} process exited immediately (exit {poll_res}); no session was created. "
            f"Log tail: tail -20 {log_file}",
            file=sys.stderr,
        )
        sys.exit(1)
    session_id = session_id or f"kilo-{agent_role}-{proc.pid}"
    quest.serf_session_id = session_id
    store.save(quest, auto_commit=auto_commit, commit_msg=f"court: goad {role_label} session {session_id} for {quest.id}")
    print(f"⚡ Goaded {role_label} for {quest.id}:")
    print(f"   Session:  {session_id} (PID {proc.pid})")
    print(f"   Agent:    {agent_role}")
    print(f"   Model:    {qual_model}")
    print(f"   Worktree: {wt}")
    print(f"   Log:      tail -f {log_file}")


# ---------------------------------------------------------------------------
# Continuation watchdog (single-turn death class): `kilo run` exits when the
# agent ends its turn — two of three cogship-253-era Gatekeeper attempts died
# exactly there, mid-task, and long Serf runs only survived via manual goads.
# The engine now spawns a detached `court watch` process beside every worker
# spawn; it waits for the worker process to exit and, when the durable quest
# state says the work is NOT done, re-prompts the SAME session
# (`kilo run --session <id>`) up to N attempts. Generalizes `court goad` to
# every role, mechanically, without babysitting the dispatching session.
# ---------------------------------------------------------------------------

WATCH_POLL_SECONDS = 5
WATCH_MAX_ATTEMPTS = 3
WATCH_WALL_CAP_SECONDS = 24 * 3600

# A worker's turn may end early ONLY while its quest sits in one of these
# pre-terminal states; anything else means the run finished its durable
# paperwork (or is human-blocked in HELD/PUNISHED) and must not be re-prompted.
_WATCH_ROLE_ACTIVE_STATES = {
    "serf": ("DISPATCHED", "WORKING"),
    "scout": ("DISPATCHED", "WORKING"),
    "gatekeeper": ("GATE",),
    "master_of_coin": ("TRIBUTE_READY",),
}


def watch_should_continue(
    role: str,
    quests: list,
    exit_code: Optional[int] = None,
) -> tuple[bool, str]:
    """Decide whether an ended worker turn needs a same-session continuation.

    Pure decision over durable state: non-zero exit always re-prompts (crashed
    or permission-killed turn); a clean exit re-prompts only while the quest
    still sits in the role's active (pre-terminal) state. Roles without a quest
    binding (artist) are never re-prompted."""
    if isinstance(exit_code, int) and exit_code != 0:
        return True, f"process exited {exit_code} before completing"
    active = _WATCH_ROLE_ACTIVE_STATES.get(role or "")
    if not quests or not active:
        return False, "no quest in an active state (turn complete or role has no quest)"
    stuck = [q.id for q in quests if q.status in active]
    if stuck:
        return True, f"{role} turn ended while {', '.join(stuck)} still in active state ({'/'.join(active)})"
    return False, "quest advanced past the role's active state (work rendered)"


def _watch_state_path(worktree: Path, role: str) -> Path:
    return worktree / ".kilo" / "watch" / f"{role}.json"


def _watch_write_state(worktree: Path, role: str, payload: dict) -> None:
    try:
        p = _watch_state_path(worktree, role)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def _watch_log(worktree: Path, role: str, line: str) -> None:
    try:
        log_file = worktree / ".kilo" / f"{role}.log"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\n--- watch ({role}): {line} ---\n")
    except OSError:
        pass


def _load_watch_quests(quest_ids: str, worktree: Optional[Path]) -> list:
    """Load the quest binding for a watch decision from the WORKTREE's own
    ledger (the freshest copy — the branch store the worker itself advances)."""
    out = []
    for qid in [x.strip() for x in (quest_ids or "").split(",") if x.strip()]:
        try:
            out.append(store.load(qid, court_root=(worktree / ".court") if worktree else None))
        except Exception:
            continue
    return out


def _process_alive(pid: Optional[int]) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def cmd_watch(args):
    """Detached continuation watchdog for an engine-spawned worker run.

    Waits for the kilo run process (pid) to exit; when the durable quest state
    says the run ended mid-task, re-prompts the SAME --session up to
    --max-attempts times. Never re-prompts human-blocked (HELD/PUNISHED) or
    completed work. State lands in .kilo/watch/<role>.json."""
    wt = Path(args.dir).resolve() if args.dir else None
    role = args.agent
    session_id = (args.session or "").strip()
    attempt = max(1, int(getattr(args, "attempt", 1)))
    max_attempts = max(1, int(args.max_attempts))
    deadline = time.time() + int(args.wall_cap)
    exclude = {x for x in (getattr(args, "exclude", "") or "").split(",") if x}

    pid = args.pid
    if pid and not _process_alive(pid):
        print(f"watch: worker pid {pid} already gone before watch started", file=sys.stderr)
        return

    # A pid-fallback spawn has no resolvable session yet: poll for it briefly
    # so a crash-free spawn can still be continued on the right session.
    if wt and not session_id and exclude:
        for _ in range(int(args.session_resolve_seconds) // 2):
            session_id = query_latest_kilo_session_id(wt, timeout_seconds=2, exclude_ids=exclude) or ""
            if session_id:
                break
            if pid and not _process_alive(pid):
                break

    while True:
        # Wait for the worker process to end its turn.
        while pid and _process_alive(pid):
            if time.time() > deadline:
                _watch_log(wt or Path.cwd(), role, f"wall cap reached; abandoning watch of {session_id or pid}")
                return
            time.sleep(WATCH_POLL_SECONDS)

        quests = _load_watch_quests(getattr(args, "quest", "") or "", wt)
        # The watchdog is not the worker's parent, so no exit code is
        # observable — the durable quest state is the completion signal.
        cont, why = watch_should_continue(role, quests, None)
        _watch_log(wt or Path.cwd(), role, f"turn ended (pid {pid}, session {session_id or '-'}): {why}")
        _watch_write_state(wt or Path.cwd(), role, {
            "role": role,
            "session_id": session_id,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "quests": [q.id for q in quests],
            "continue": cont,
            "reason": why,
            "ts": now_iso(),
        })
        if not cont:
            return
        if attempt >= max_attempts:
            _watch_log(wt or Path.cwd(), role, f"attempt {attempt}/{max_attempts} still incomplete — manual goad required")
            print(f"watch: attempt {attempt}/{max_attempts} ended mid-task ({why}); manual `court goad` required", file=sys.stderr)
            return
        if not (session_id or (wt and exclude)):
            _watch_log(wt or Path.cwd(), role, "no resolvable session id — cannot re-prompt the same session")
            return

        # Re-prompt the SAME session with a continuation of the original task.
        task_text = ""
        for tf in ((getattr(args, "task_file", "") or "TASK.md"), "TASK.md"):
            if wt and tf:
                p = wt / ".kilo" / tf
                if p.is_file():
                    try:
                        task_text = p.read_text(encoding="utf-8")
                    except OSError:
                        task_text = ""
                    break
        continuation = (
            "Your previous turn ended before the assigned task was complete "
            f"(continuation attempt {attempt + 1} of {max_attempts}). Continue from where you "
            "stopped and drive the task to completion end-to-end: finish the deliverables, run "
            "the required verifications, and complete the durable paperwork for your role "
            "before ending your turn. Do not end the turn with the task unfinished.\n\n"
            "Original task:\n\n" + (task_text.strip() or "(see .kilo/TASK.md in this worktree)")
        )
        kilo_bin = find_kilo_binary()
        if not kilo_bin:
            _watch_log(wt or Path.cwd(), role, "kilo binary not found — cannot re-prompt")
            return
        target_session = session_id or query_latest_kilo_session_id(wt, timeout_seconds=5, exclude_ids=exclude) or ""
        if not target_session:
            _watch_log(wt or Path.cwd(), role, "no resolvable session id — cannot re-prompt the same session")
            return
        cmd = [
            str(kilo_bin), "run",
            "--agent", role,
            "--dir", str(wt or Path.cwd()),
            "--session", target_session,
            "--auto",
            continuation,
        ]
        if args.model:
            cmd += ["--model", args.model]
        log_file = (wt or Path.cwd()) / ".kilo" / f"{role}.log"
        (wt or Path.cwd()).mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\n--- watch continuation {attempt + 1}/{max_attempts} for {role} ({datetime.now().isoformat()}) ---\n")
        log_out = open(log_file, "a", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(wt or Path.cwd()),
                stdout=log_out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=_engine_spawn_env(wt),
            )
        finally:
            log_out.close()
        attempt += 1
        pid = proc.pid


def _spawn_watchdog(
    worktree_path: Path,
    role: str,
    pid: int,
    session_id: str,
    model: str,
    quest_ids: str = "",
    task_file: str = "TASK.md",
    exclude_ids: Optional[set] = None,
    max_attempts: int = WATCH_MAX_ATTEMPTS,
) -> Optional[int]:
    """Launch the detached continuation watchdog for a freshly spawned worker.
    Returns the watchdog pid, or None when disabled (COURT_WATCH_DISABLE=1) or
    unspawnable (tests, no python, ...). Never raises."""
    if os.environ.get("COURT_WATCH_DISABLE") == "1":
        return None
    cmd = [
        sys.executable, "-m", "court.cli", "watch",
        "--pid", str(pid),
        "--dir", str(worktree_path),
        "--agent", role,
        "--model", model,
        "--session", session_id or "",
        "--task-file", task_file,
        "--max-attempts", str(max_attempts),
        "--exclude", ",".join(sorted(exclude_ids or set())),
        "--attempt", "1",
    ]
    if quest_ids:
        cmd += ["--quest", quest_ids]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(_court_package_root(worktree_path) or worktree_path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=_engine_spawn_env(worktree_path),
        )
        return proc.pid
    except Exception:
        return None


def cmd_artist_say(args):
    """Deliver M'Lord's instruction to an existing Court Artist session.

    Mirrors the goad path: runs one whole agent turn via the Kilo CLI against
    the session recorded on the Quest (artist_session_id), streaming output.
    """
    try:
        quest = store.load(args.quest_id)
    except Exception as e:
        print(f"ERROR: {args.quest_id}: {e}", file=sys.stderr)
        sys.exit(1)

    wt = git_ops.find_worktree_for_quest(quest)
    if not wt or not wt.is_dir():
        print(
            f"ERROR: No active worktree found on disk for {quest.id} (branch: {quest.branch or '-'})",
            file=sys.stderr,
        )
        sys.exit(1)

    session_id = (getattr(quest, "artist_session_id", "") or "").strip()
    if not session_id:
        print(
            f"ERROR: {quest.id} has no recorded Court Artist session — the frontmatter field "
            f"`artist_session_id` is empty. Summon one first (`court artist {quest.id}` or "
            f"`court studio <ids> --standup`), then record it: "
            f"python3 -m court.cli set-field {quest.id} artist_session_id <session_id>",
            file=sys.stderr,
        )
        sys.exit(1)

    kilo_bin = find_kilo_binary()
    if not kilo_bin:
        print("ERROR: Kilo binary not found. Cannot deliver the instruction via CLI.", file=sys.stderr)
        sys.exit(1)

    model = quest.artist_model or config.get_model("artist")
    qual_model = canonical_model_id(model)
    cmd = [
        str(kilo_bin),
        "run",
        "--dir", str(wt),
        "--agent", "artist",
        "--session", session_id,
        "--model", qual_model,
        args.instruction,
    ]
    print(f"🎨 Delivering instruction to Court Artist session {session_id} ({quest.id}, model {qual_model})…")
    result = subprocess.run(cmd, env=_engine_spawn_env(wt))
    raise SystemExit(result.returncode)


def _extract_target_routes(quest: Quest, worktree: Optional[Path] = None) -> list[str]:
    """Extract live UI preview routes from the Quest's Tally or touched templates."""
    routes: list[str] = []
    tally = quest.extract_tribute_subsection("tally")
    if tally:
        for line in tally.splitlines():
            found = re.findall(r"(?:https?://[^\s/]+)?(/[a-zA-Z0-9_\-./]+)", line)
            for p in found:
                p_clean = p.rstrip(").,;:*`'")
                if p_clean.endswith((".py", ".md", ".json", ".sql", ".sh", ".csv", ".log", ".txt", ".png", ".jpg", ".svg")):
                    continue
                if p_clean.startswith(("/Users", "/home", "/var", "/tmp", "/etc", "/private", "/opt", "/venv")):
                    continue
                if len(p_clean) > 1 and p_clean not in routes:
                    routes.append(p_clean)

    if worktree and worktree.is_dir():
        try:
            res = subprocess.run(
                ["git", "diff", "--name-only", "castle...HEAD"],
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    if line.startswith("templates/") and not routes:
                        routes.append(f"/{quest.app}/" if quest.app else "/")
                        break
        except Exception:
            pass

    if not routes:
        routes.append(f"/{quest.app}/" if quest.app else "/")

    return routes


def _ensure_worktree_server(
    worktree: Path,
    port_override: Optional[int] = None,
    no_server: bool = False,
) -> tuple[int, str, str]:
    """Ensure a development server is running for a worktree.
    Returns (port, runserver_url, status_description).
    """
    if port_override:
        return port_override, f"http://localhost:{port_override}", "Manual port override"

    port_file = worktree / ".worktree-port"
    port = None

    if not no_server and MANAGE_SERVERS_PATH.exists():
        try:
            subprocess.run(
                ["bash", str(MANAGE_SERVERS_PATH), "start", str(worktree)],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except Exception:
            pass

    if port_file.exists():
        try:
            port = int(port_file.read_text().strip())
        except Exception:
            pass

    if not port and MANAGE_SERVERS_PATH.exists():
        try:
            res = subprocess.run(
                ["bash", str(MANAGE_SERVERS_PATH), "get_port", str(worktree)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip().isdigit():
                port = int(res.stdout.strip())
        except Exception:
            pass

    if not port:
        port = 8000

    status = "Active" if not no_server else "Server startup skipped (--no-server)"
    return port, f"http://localhost:{port}", status


def cmd_artist(args):
    """Spawn or prepare a dedicated Court Artist session with runserver for interactive UI review."""
    quest = store.load(args.quest_id)
    wt = git_ops.find_worktree_for_quest(quest)
    if not wt or not wt.is_dir():
        print(
            f"ERROR: No active worktree found on disk for {quest.id} (branch: {quest.branch or '-'}). "
            "A Court Artist session requires a live worktree to host the dev server and codebase.",
            file=sys.stderr,
        )
        sys.exit(1)

    model = getattr(args, "model", None) or quest.artist_model or config.get_model("artist")
    provider = getattr(args, "provider", None) or config.get_provider("artist")
    port, runserver_url, server_status = _ensure_worktree_server(
        wt,
        port_override=getattr(args, "port", None),
        no_server=getattr(args, "no_server", False),
    )
    routes = _extract_target_routes(quest, worktree=wt)

    tmpl_path = REPO_ROOT / ARTIST_DISPATCH_TEMPLATE
    if not tmpl_path.exists():
        tmpl_path = Path(__file__).resolve().parent.parent / "templates" / "court_artist_prompt.md"

    if tmpl_path.exists():
        tmpl_text = tmpl_path.read_text(encoding="utf-8")
    else:
        tmpl_text = "You are the Court Artist for {{ quest_id }}. Worktree: {{ worktree }}. Runserver: {{ runserver_url }}"

    routes_str = "\n".join(f"- {runserver_url}{r}" if not r.startswith("http") else f"- {r}" for r in routes)
    prompt = (
        tmpl_text
        .replace("{{ quest_id }}", quest.id)
        .replace("{{ quest_title }}", quest.title)
        .replace("{{ worktree }}", str(wt))
        .replace("{{ branch }}", quest.branch or "-")
        .replace("{{ app }}", quest.app or "common")
        .replace("{{ concern }}", quest.concern or "ui")
        .replace("{{ runserver_url }}", runserver_url)
        .replace("{{ port }}", str(port))
        .replace("{{ target_routes }}", routes_str)
    )

    # Record model and Castle Ledger note
    quest.artist_model = model
    quest.log_ledger(
        quest.status,
        quest.status,
        f"Court Artist summoned for UI review with model {model} (runserver on port {port})",
    )
    auto_commit = not getattr(args, "no_commit", False)
    store.save(quest, auto_commit=auto_commit, commit_msg=f"court: summon artist for {quest.id}")

    short_id = quest.id.split("-")[0]
    # Single unified spawn path (2026-09-28): Agent Manager prompting is
    # retired — headless Kilo CLI sessions are the one documented way to
    # launch the Court Artist. The brief is written to the worktree so the
    # printed `kilo run` line and the JSON spawn payload carry it verbatim.
    brief_file = wt / ".kilo" / "TASK_ARTIST.md"
    try:
        brief_file.parent.mkdir(parents=True, exist_ok=True)
        brief_file.write_text(prompt, encoding="utf-8")
    except Exception:
        brief_file = None
    spawn_desc = {
        "argv": [
            "kilo", "run",
            "--agent", "artist",
            "--model", model,
            "--dir", str(wt),
            *([f"$(cat {brief_file})"] if brief_file else [prompt]),
        ],
        "interactive": f"cd {wt} && kilo   # default_agent already set to artist",
        "model": model,
        "provider": provider,
    }

    if getattr(args, "prompt_only", False):
        print(prompt)
        return

    if getattr(args, "json", False):
        out = {
            "quest_id": quest.id,
            "title": quest.title,
            "branch": quest.branch,
            "worktree": str(wt),
            "port": port,
            "runserver_url": runserver_url,
            "model": model,
            "provider": provider,
            "routes": routes,
            "prompt": prompt,
            "spawn": spawn_desc,
        }
        print(json.dumps(out, indent=2))
        return

    print("=" * 76)
    print("🎨 COURT ARTIST SUMMONED — ROYAL UI REVIEW STUDIO")
    print("=" * 76)
    print(f"Quest:       {quest.id}")
    print(f"Title:       {quest.title}")
    print(f"Branch:      {quest.branch or '-'}")
    print(f"Worktree:    {wt}")
    print(f"Runserver:   {runserver_url} (Port {port}) [{server_status}]")
    print(f"Model:       {model} ({provider})")
    print()
    print("Live Preview URLs:")
    for r in routes:
        url_line = f"{runserver_url}{r}" if not r.startswith("http") else r
        print(f"  • {url_line}")
    print()
    print("Next Steps for M'Lord & Steward:")
    print("  1. Launch the Court Artist session via Kilo CLI (the single unified path —")
    print("     Agent Manager prompting is retired; it defaults to steward mode and lacks")
    print("     agent parameterization):")
    if brief_file:
        print(f"     kilo run --agent artist --model \"{model}\" --dir {wt} \"$(cat {brief_file})\"")
    else:
        print(f"     kilo run --agent artist --model \"{model}\" --dir {wt} \"<the brief>\"")
    print(f"     (or interactive: cd {wt} && kilo — default_agent already set to artist).")
    print(f"     (Slash command: `/artist {quest.id}` drives this flow.)")
    print(f"  2. Open the live preview in your browser: {runserver_url}")
    print("  3. Direct the Court Artist on layout, typography, colors, and design system compliance.")
    print("  4. The Court Artist will edit templates live and prompt you to refresh.")
    print("  5. When satisfied, the Court Artist signs the Tally and commits changes.")
    print("=" * 76)


def cmd_verify(args):
    quest = store.load(args.quest_id)
    if not quest.worktree:
        print(f"ERROR: {quest.id} has no worktree path set (use set-field)", file=sys.stderr)
        sys.exit(1)

    report_lines = [f"## Verify run ({now_iso()})"]
    status = git_ops.worktree_status(quest.worktree)
    report_lines.append(f"- worktree_status: {status}")

    if args.test_cmd:
        test_result = git_ops.run_test_command(quest.worktree, args.test_cmd, timeout=args.timeout)
        report_lines.append(f"- test_cmd: `{args.test_cmd}`")
        report_lines.append(f"- exit_code: {test_result.get('exit_code')}")
        report_lines.append(f"- ok: {test_result.get('ok')}")
        if test_result.get("stdout"):
            report_lines.append(f"```\n{test_result['stdout'][-1500:]}\n```")
        if test_result.get("stderr"):
            report_lines.append(f"stderr:\n```\n{test_result['stderr'][-1500:]}\n```")

    quest.set_section("Tribute Rendered", "\n".join(report_lines), mode="append")
    quest.log_ledger(quest.status, quest.status, "Deterministic verify run recorded")
    auto_commit = not getattr(args, "no_commit", False)
    store.save(quest, auto_commit=auto_commit, commit_msg=f"court: verify {quest.id}")
    print("\n".join(report_lines))


def _format_merge_status_card(target_label: str, status: dict) -> str:
    lines = [
        "=" * 72,
        f"MERGE & DIFFERENCING VERIFICATION: {target_label}",
        "=" * 72,
        f"Branch:           {status.get('branch') or '-'}",
        f"Worktree:         {status.get('worktree') or '(none on disk)'}",
        f"Target Ref:       {status.get('target_ref')}",
        f"Base Ref:         {status.get('base_ref')}",
        "",
        f"STATUS:           {'✅ MERGED' if status.get('is_merged') else ('🚨 DIRTY WORKTREE' if not status.get('clean_worktree') else '⚠️ UNMERGED')}",
        f"Merged in Target: {'✅ YES' if status.get('is_merged_target') else '❌ NO'} (ancestor={status.get('is_ancestor_target')})",
        f"Merged in Base:   {'✅ YES' if status.get('is_merged_base') else '❌ NO'} (ancestor={status.get('is_ancestor_base')})",
        f"Worktree Clean:   {'✅ YES' if status.get('clean_worktree') else '🚨 DIRTY (' + str(len(status.get('uncommitted_files', []))) + ' files)'}",
        f"Unmerged Commits: {status.get('unmerged_commits_count', 0)} vs {status.get('target_ref')} ({status.get('unmerged_commits_base_count', 0)} vs {status.get('base_ref')})",
        f"Pending Diff:     {'⚠️ YES' if status.get('has_diff') else '✅ NONE'}",
        "",
        f"Recommendation:   {status.get('recommendation')}",
    ]

    if status.get("uncommitted_files"):
        lines.append(f"\n[Uncommitted / Untracked Files ({len(status['uncommitted_files'])})]")
        for f in status["uncommitted_files"]:
            lines.append(f"  {f}")

    if status.get("unmerged_commits"):
        lines.append(f"\n[Unmerged Commits vs {status.get('target_ref')} ({len(status['unmerged_commits'])})]")
        for c in status["unmerged_commits"]:
            lines.append(f"  - {c.get('hash')} {c.get('message')}")

    if status.get("diff_stat"):
        lines.append(f"\n[Diffstat vs {status.get('target_ref')}]")
        for dline in status["diff_stat"].splitlines():
            lines.append(f"  {dline}")

    return "\n".join(lines)


def cmd_verify_merged(args):
    target_ref = getattr(args, "target_ref", None) or "gatehouse"
    base_ref = getattr(args, "base_ref", None) or "castle"
    target_arg = getattr(args, "quest_id", None) or getattr(args, "target", None)

    if getattr(args, "include_archived", False) or not target_arg:
        quests = store.list_all(include_archive=getattr(args, "include_archived", False))
        if getattr(args, "status", None):
            quests = [q for q in quests if q.status == args.status]
        if not quests:
            print("(no matching quests to verify)")
            return

        if getattr(args, "json", False):
            results = []
            for q in quests:
                target = q.branch or q.worktree
                res = git_ops.check_merged_status(target, target_ref=target_ref, base_ref=base_ref)
                res["quest_id"] = q.id
                res["quest_title"] = q.title
                res["quest_status"] = q.status
                results.append(res)
            print(json.dumps(results, indent=2))
            return

        print("=" * 72)
        print(f"THE COURT — Merge & Differencing Audit ({len(quests)} Quests)")
        print("=" * 72)
        for q in quests:
            target = q.branch or q.worktree
            res = git_ops.check_merged_status(target, target_ref=target_ref, base_ref=base_ref)
            is_scout = (q.kind == "scout" or q.section == "Investigation")

            if res.get("is_merged") and res.get("is_merged_base"):
                badge = "✅ MERGED(gatehouse+castle)"
            elif res.get("is_merged"):
                badge = "🟡 MERGED(gatehouse)"
            elif not res.get("clean_worktree"):
                badge = f"🚨 DIRTY({len(res.get('uncommitted_files', []))} files)"
            elif is_scout:
                badge = "🔭 SCOUT(non-merging)"
            else:
                badge = f"⚠️ UNMERGED({res.get('unmerged_commits_count', 0)} commits)"

            print(f"[{badge:26}] {q.id:38} [{q.status:18}]")
            print(f"    branch={q.branch or '-'} | {res.get('recommendation')}")
        return

    # Single target verification
    target_name = target_arg
    target_branch_or_path = target_arg
    quest_obj = None

    try:
        quest_obj = store.load(target_arg)
        target_name = f"{quest_obj.id} ({quest_obj.title})"
        target_branch_or_path = quest_obj.branch or quest_obj.worktree
    except Exception:
        pass

    status = git_ops.check_merged_status(target_branch_or_path, target_ref=target_ref, base_ref=base_ref)
    if quest_obj:
        status["quest_id"] = quest_obj.id
        status["quest_title"] = quest_obj.title
        status["quest_status"] = quest_obj.status

    if getattr(args, "sync", False) and status.get("worktree") and Path(status["worktree"]).exists():
        wt_path = Path(status["worktree"])
        sync_res = git_ops._run(["git", "merge", base_ref, "--ff-only"], wt_path)
        status["synced"] = sync_res.get("ok")

    if getattr(args, "json", False):
        print(json.dumps(status, indent=2))
    else:
        print(_format_merge_status_card(target_name, status))

    if getattr(args, "strict", False) and not status.get("is_merged"):
        sys.exit(1)


def _code_presence_check(quest: Quest, trunk: str = "castle") -> dict:
    """Post-promotion code-presence audit for one Quest's branch (cogship-076/077).
    Scouts are exempt (non-merging lane). Returns git_ops.verify_quest_promotion's dict."""
    quest_branch = quest.branch or getattr(quest, "tree_branch", "")
    if not quest_branch:
        return {"ok": False, "reason": f"{quest.id} has no resolvable branch to verify"}
    return git_ops.verify_quest_promotion(quest_branch, trunk=trunk)


def cmd_verify_manifest(args):
    """cogship-077 fix: after any convoy promotion, deterministically assert each
    manifest Quest's BRANCH TIP is an ancestor of the promoted trunk AND carries
    production content — one merge-base + diff per quest. This is the single
    check that would have caught all six affected quests."""
    cogship = getattr(args, "cogship", None)
    if cogship:
        quests = [q for q in store.list_all(include_archive=True)
                  if store.normalize_cogship_id(q.cogship_id) == (store.normalize_cogship_id(cogship) or cogship)]
        if not quests:
            print(f"ERROR: no Quests stamped onto {cogship}", file=sys.stderr)
            sys.exit(1)
    else:
        quests = resolve_quest_selection(args, batchable=True, required=True)

    trunk = getattr(args, "trunk", "castle") or "castle"
    failures = []
    print(f"🔎 POST-PROMOTION MANIFEST VERIFICATION against '{trunk}' ({len(quests)} Quest(s))")
    for q in quests:
        if q.kind == "scout" or q.section == "Investigation":
            print(f"  ⏭️  {q.id} (scout — non-merging lane, exempt)")
            continue
        promo = _code_presence_check(q, trunk=trunk)
        if promo.get("ok"):
            print(f"  ✅ {q.id} tip {promo['tip'][:12]} ancestor of {trunk}; {len(promo['production_files'])} production file(s)")
        else:
            failures.append((q.id, promo.get("reason")))
            print(f"  🔴 {q.id} — {promo.get('reason')}")
    if failures:
        print(f"\n❌ {len(failures)}/{len(quests)} manifest Quest(s) FAILED promotion verification — the trunk does not actually contain this convoy's code.")
        sys.exit(1)
    print(f"\n✅ All {len(quests)} manifest Quest(s) verified: branch tips reachable from {trunk} with production content present.")


def cmd_raze(args):
    import json
    am_path = agent_manager_json_path()
    am_data = {}
    if am_path.exists():
        try:
            am_data = json.loads(am_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    am_wts = am_data.get("worktrees", {})
    am_sessions = am_data.get("sessions", {})
    ashes_section_id = None
    for sec_id, sec_data in am_data.get("sections", {}).items():
        if sec_data.get("name") == "Ashes":
            ashes_section_id = sec_id
            break

    # Q185: retired the old magic quest_id strings ("all"/"all-ready"/"ready")
    # in favor of the shared selector — they were invisible in `--help`, a
    # plain string comparison inside this function body, and easy to typo.
    # Omitting quest_ids (with no --status) reproduces the exact same
    # default set the magic strings used to mean.
    target_quests = resolve_quest_selection(
        args, batchable=True, required=False,
        default_status="READY_TO_RAZE",
    )
    target_ids = [q.id for q in target_quests]

    if not target_ids:
        print("(no candidate quests found to raze)")
        return

    already_razed_in_run: set[str] = set()

    def _raze_one(qid: str) -> None:
        if qid in already_razed_in_run:
            return
        already_razed_in_run.add(qid)
        try:
            quest = store.load(qid)
        except Exception as e:
            print(f"Skipping {qid}: {e}")
            return

        # Pillory linkage guard: a Punished Quest's frozen worktree is never
        # razed on its own — only once its successor has actually completed,
        # then both are razed together in the same pass.
        if quest.status == "PUNISHED":
            successor_id = quest.pilloried_by
            if not successor_id:
                print(f"⏸️  {quest.id}: PUNISHED with no successor chartered yet — nothing to raze. Charter a successor and set pilloried_by first.")
                return
            try:
                successor = store.load(successor_id)
            except Exception:
                print(f"⏸️  {quest.id}: successor {successor_id} not found — cannot raze yet.")
                return
            if successor.status not in ("READY_TO_RAZE", "DONE"):
                print(f"⏸️  {quest.id}: frozen, awaiting successor {successor_id} (currently [{successor.status}]) — raze together once it completes.")
                return
            # Successor is done: the frozen worktree was rejected and never
            # merged, so skip merge/ff-only checks entirely — just close it out.
            if quest.status != "READY_TO_RAZE":
                quest.log_ledger(quest.status, "READY_TO_RAZE", f"Razed alongside completed successor {successor_id}: pillory lineage closed out.")
                quest.status = "READY_TO_RAZE"
                store.save(quest)
            print(f"🔥🔒 Razed pilloried {quest.id} alongside completed successor {successor_id}.")
            return

        branch = quest.branch or ""
        wt_path_str = quest.worktree or ""
        found_wt_id = None
        found_wt_path = None
        found_session_id = None

        # Resolve worktree and session from AM data
        for wid, wdata in am_wts.items():
            if wdata.get("branch") == branch or (wt_path_str and (wdata.get("path") == wt_path_str or wid == wt_path_str)):
                found_wt_id = wid
                found_wt_path = wdata.get("path")
                break

        if found_wt_id:
            for sid, sdata in am_sessions.items():
                if sdata.get("worktreeId") == found_wt_id:
                    found_session_id = sid
                    break

        wt_exists = bool(found_wt_path and Path(found_wt_path).exists())

        # Direct filesystem fallback: the AM JSON lookup above matches by exact
        # string equality (branch/path) and can miss a live worktree entirely on
        # a renamed branch or stale agent-manager.json. Before concluding "already
        # pruned" below, trust the filesystem over that lookup: if quest.worktree
        # still points to a real directory containing a .git entry, the worktree
        # is NOT pruned, regardless of whether the AM JSON lookup found it. This
        # auto-archived a live quest with an active idle session still attached
        # (Q146, 2026-09-04) — the fix is to fall back to checking disk directly.
        if not wt_exists and not found_wt_id and wt_path_str:
            fs_wt_path = Path(wt_path_str)
            if fs_wt_path.exists() and (fs_wt_path / ".git").exists():
                print(
                    f"⚠️ {quest.id}: Agent Manager lookup missed this worktree "
                    f"(stale agent-manager.json or renamed branch?), but {wt_path_str} "
                    f"exists on disk with a live .git — treating as NOT pruned."
                )
                found_wt_path = wt_path_str
                wt_exists = True

        # If worktree does not exist anywhere and was already deleted
        if not wt_exists and not found_wt_id:
            if quest.status == "READY_TO_RAZE" and args.archive_pruned:
                try:
                    dst = store.archive(quest.id)
                    print(f"🪦 {quest.id}: Worktree already pruned from disk/AM -> Archived to {dst}")
                except FileNotFoundError:
                    print(f"🪦 {quest.id}: Worktree pruned and quest already in archive.")
                return
            elif quest.status == "READY_TO_RAZE":
                print(f"ℹ️ {quest.id}: Worktree already pruned from disk/AM (ready to archive: python3 -m court.cli archive {quest.id})")
                return

        # Check merge status. Scouts are non-merging by design (Investigation
        # lane): their branch never enters gatehouse/castle, so the merge gate
        # must not block their teardown — findings live in durable artifacts
        # and the Scout Report, not in merged commits.
        is_scout = (quest.kind == "scout" or quest.section == "Investigation")
        status = git_ops.check_merged_status(found_wt_path or branch, target_ref="castle", base_ref="castle")
        is_merged = status.get("is_merged_target") or status.get("is_merged_base")

        if not is_merged and not is_scout:
            unmerged_count = status.get("unmerged_commits_count", 0)
            print(
                f"❌ {quest.id}: NOT merged into castle (verified via merge-base ancestor check; "
                f"{unmerged_count} unmerged commit(s)). Refusing to raze. Route through Gatekeeper first."
            )
            return

        # cogship-076/077 gate: a FORCED promotion marker means the normal
        # merge path was bypassed — require the code-presence audit to pass
        # (branch tip reachable from castle AND production content on the
        # branch) before the branch ref is deleted. Ref deletion is what made
        # the shared-cache quest's branch unrecoverable except via the object
        # graph. `--allow-forced` is the explicit royal override.
        if (
            not is_scout
            and _last_ledger_entry_is_forced(quest)
            and not getattr(args, "allow_forced", False)
        ):
            promo = _code_presence_check(quest)
            if not promo.get("ok"):
                print(
                    f"❌ {quest.id}: FORCED promotion marker present and code-presence audit FAILED:\n"
                    f"   {promo.get('reason')}\n"
                    f"   Refusing to raze (raze deletes the branch ref — unrecoverable if the audit is wrong).\n"
                    f"   Fix the promotion, or pass --allow-forced to raze anyway (explicit override)."
                )
                return

        # Sync worktree to castle if it exists (skip for scouts: their unique
        # spike commits are never merged, so a --ff-only/reset sync would either
        # fail or destroy the spike history before archival).
        if wt_exists and found_wt_path:
            p = Path(found_wt_path)
            # Check clean status
            st_res = git_ops._run(["git", "status", "--porcelain"], p)
            if st_res.get("ok") and st_res.get("stdout"):
                print(f"⚠️ {quest.id}: Worktree {found_wt_path} is dirty with uncommitted changes! Clean before razing.")
                return

            if not is_scout:
                # Fast forward to castle
                ff_res = git_ops._run(["git", "merge", "castle", "--ff-only"], p)
                if not ff_res.get("ok"):
                    # Try reset if branch is already merged into castle
                    if is_merged:
                        git_ops._run(["git", "reset", "--hard", "castle"], p)

        # Advance quest to READY_TO_RAZE if not already
        if quest.status != "READY_TO_RAZE":
            if is_scout:
                note = "Razed (scout, non-merging): clean tree verified, findings extracted to durable artifacts, queued for teardown in Ashes"
            else:
                note = "Razed: verified merged, synced to castle (ahead: 0, behind: 0), queued for teardown in Ashes"
            quest.log_ledger(quest.status, "READY_TO_RAZE", note)
            quest.status = "READY_TO_RAZE"
            store.save(quest)
            print(f"✅ Advanced {quest.id} -> READY_TO_RAZE")

        print(f"🔥 Razed {quest.id}:")
        print(f"   - Branch: {branch}")
        print(f"   - Worktree: {found_wt_id} ({found_wt_path})")
        print(f"   - Session ID: {found_session_id or quest.serf_session_id or 'None'}")
        print(f"   - Ashes Section ID: {ashes_section_id}")
        if found_session_id and ashes_section_id:
            print(f"   👉 Stop the idle session via Kilo CLI: kilo session delete {found_session_id} (Ashes section: {ashes_section_id}; M'Lord prunes the directory by hand)")

        # This Quest is a pillory successor: raze its frozen predecessor
        # together with it, in the same pass, per the Pillory protocol.
        if quest.pillory_of:
            print(f"   🔗 {quest.id} is a pillory successor of {quest.pillory_of} — razing it together now.")
            _raze_one(quest.pillory_of)

    for qid in target_ids:
        _raze_one(qid)


def cmd_runsuite(args):
    """Engine-executed unified integration suite (cogship-082 fix). The suite
    runs HERE — inside court's own subprocess with a proper long timeout —
    never inside an agent's shell tool (whose 120s cap killed every real run
    and coerced the Gatekeeper into quoting Tribute text as "proof"). Stamps
    durable proof that the READY_TO_RAZE gate independently re-verifies."""
    cogship = getattr(args, "cogship", None) or ""
    quests = []
    if getattr(args, "quest_id", None):
        quests = [store.load(args.quest_id)]
    elif cogship:
        quests = store.list_all()
        cog_norm = store.normalize_cogship_id(cogship) or cogship
        quests = [q for q in quests if store.normalize_cogship_id(q.cogship_id) == cog_norm]
        if not quests:
            print(f"ERROR: no Quests stamped onto {cogship}", file=sys.stderr)
            sys.exit(1)

    wt_arg = getattr(args, "dir", None)
    if wt_arg:
        wt = Path(wt_arg).resolve()
    elif len(quests) == 1 and quests[0].worktree and Path(quests[0].worktree).is_dir():
        wt = Path(quests[0].worktree)
    elif len(quests) > 1:
        print(
            "ERROR: convoy of "
            f"{len(quests)} Quest(s) runs in the ephemeral gatehouse worktree — "
            f"pass --dir <gatehouse worktree path> (branch the-gatehouse/{cogship})",
            file=sys.stderr,
        )
        sys.exit(1)
    else:
        print("ERROR: no worktree resolved — pass --dir <worktree> or a quest id", file=sys.stderr)
        sys.exit(1)

    res = git_ops.run_unified_suite(
        wt,
        command=getattr(args, "command", None),
        cogship_id=cogship,
        quest_id=(quests[0].id if len(quests) == 1 else ""),
        timeout=getattr(args, "timeout", 1800),
    )
    if getattr(args, "json", False):
        print(json.dumps(res, indent=2))
    else:
        if res.get("proof_path"):
            print(f"🧾 Proof stamped: {res['proof_path']}")
        if res.get("test_db_recreated"):
            print(f"♻️  Stale kept test DB rebuilt ({res['test_db_recreated']})")
        if res.get("ok"):
            print(f"✅ Suite PASSED ({res.get('ran_tests')} tests, exit 0) at {res.get('head_sha', '')[:12]} — {res.get('command')}")
        else:
            print(f"❌ Suite FAILED (exit {res.get('exit_code')}) — {res.get('error') or 'see output above'}")
            sys.exit(1)


def cmd_collect(args):
    """Composite (Q185): mechanizes `collect.md`'s steps 1-2 — audit-and-select,
    stamp the Cog Ship convoy, batch-advance to GATE — the pure CLI plumbing
    with no judgment call in it. Step 3 summons the Gatekeeper session
    on the gatehouse convoy branch. One command replaces collect.md's
    manual audit + stamp + per-Quest advance loop.

    This is also where the 2026-09-05 incident's actual gap gets mechanically
    closed, not just discouraged in prose: a Quest with no real Master of
    Coin audit content recorded on disk is REFUSED here, never packed into
    a convoy — the exact thing that let 9 Quests skip straight from
    TRIBUTE_READY to GATE with an empty '## Master of Coin's Audit' section.

    Gatehouse routing (2026-09-05 re-architecture, same session as the
    store.py event-log rewrite): the persistent named-station model
    (`the-gatehouse/north|south|east|west`) is retired. Agent Manager has no
    way to attach a fresh session to an already-existing, sessionless
    worktree — only create new — which is exactly why those 4 stations kept
    going stale (66-751 commits behind castle) between convoys: nothing
    fast-forwards a dormant worktree with no session running in it. The
    replacement routes on convoy size instead of a fixed station name:
    a size-1 convoy runs Gatekeeper directly in that one Quest's own
    worktree (nothing to batch, no reason for a separate integration point);
    a size>1 convoy gets a brand-new ephemeral worktree scoped to that one
    convoy only (`the-gatehouse/<cogship_id>`), torn down after promotion.
    """
    candidates = resolve_quest_selection(args, batchable=True, required=True, default_status="TRIBUTE_READY")
    base_branch = getattr(args, "base", "castle") or "castle"
    auto_commit = not getattr(args, "no_commit", False)

    accepted: list[Quest] = []
    skipped: list[tuple[str, str]] = []
    scan_warnings: list[str] = []
    repo_root = git_ops.get_repo_root()
    for quest in candidates:
        if quest.status == "PUNISHED":
            skipped.append((quest.id, "PUNISHED (side-state, frozen pending its pillory successor)"))
            continue
        if quest.status not in ("TRIBUTE_READY", "GATE"):
            skipped.append((quest.id, f"status is [{quest.status}], not TRIBUTE_READY or GATE"))
            continue
        # Gates read the worktree charter first (freshest: coin verdicts and
        # UI-review approvals commit on the branch before any convoy promotes),
        # falling back to the castle copy when no worktree exists.
        wt_quest = _quest_worktree_charter(quest)
        effective_quest = wt_quest if wt_quest is not None else quest
        moc_audit = effective_quest.body_sections.get("Master of Coin's Audit", "").strip() or quest.body_sections.get("Master of Coin's Audit", "").strip()
        is_hotfix = quest.is_hotfix or (getattr(args, "hotfix", False) is True)
        if not moc_audit:
            if is_hotfix:
                stat_text = ""
                if quest.worktree and Path(quest.worktree).is_dir():
                    diff_info = git_ops.get_worktree_diff(quest.worktree, base=base_branch, stat_only=True)
                    stat_text = diff_info.get("diff", "").strip()
                diff_block = f"\n- **Diff Footprint**:\n```\n{stat_text}\n```\n" if stat_text else "\n"
                audit_text = (
                    "### Master of Coin Audit (Hotfix Fast-Track)\n"
                    "- **Verdict**: PASS (Hotfix inline verified)\n"
                    "- **Commutation**: None required / Deploy immediately\n"
                    "- **UI Review**: None required"
                    f"{diff_block}"
                )
                quest.set_section("Master of Coin's Audit", audit_text)
                store.save(quest, auto_commit=auto_commit, commit_msg=f"court: inline hotfix audit {quest.id}")
            else:
                skipped.append((
                    quest.id,
                    "no recorded Master of Coin's Audit content -- not yet reviewed; refusing to "
                    "pack unaudited tribute into a convoy (dispatch `/levy <id>` first)",
                ))
                continue
        ui_status = effective_quest.extract_ui_review_status() or quest.extract_ui_review_status()
        if ui_status.upper().startswith("PENDING") and not getattr(args, "skip_ui_review", False) and not is_hotfix:
            skipped.append((
                quest.id,
                f"UI Review is PENDING ({ui_status}) — royal review required before collection "
                f"(recommend `/artist {quest.id}` or pass `--skip-ui-review`)",
            ))
            continue
        audit = ward.audit_quest(quest, base_branch=base_branch)
        if audit.git_status.get("dirty"):
            skipped.append((quest.id, "dirty working tree"))
            continue
        if audit.violations:
            skipped.append((quest.id, f"{len(audit.violations)} compliance violation(s): {'; '.join(audit.violations)}"))
            continue
        # Shape C gate (2026-09-15 migration-fork class): refuse to pack quest-local
        # merge migrations. Differential scan - only files the branch ADDS over the
        # trunk, so castle's own pre-existing merge nodes never flag innocent branches.
        if quest.branch:
            try:
                contraband = migration_guard.scan_branch_contraband(repo_root, base_branch, quest.branch)
            except Exception as e:
                scan_warnings.append(f"{quest.id}: migration contraband scan failed ({e}); Gatekeeper graph check remains the backstop")
            else:
                if contraband:
                    skipped.append((quest.id, migration_guard.remediation_message(contraband, base_branch)))
                    continue
        accepted.append(quest)

    if skipped:
        print(f"⏭️  Skipped {len(skipped)} candidate(s) (not ready to pack):")
        for qid, reason in skipped:
            print(f"   - {qid}: {reason}")

    for warning in scan_warnings:
        print(f"⚠️  {warning}")

    if not accepted:
        print("(no Master-of-Coin-approved Quests ready to pack into a Cog Ship)")
        return

    # Single-writer serialization for the convoy-minting tail: stamp + standup
    # is where double-running operators minted duplicate convoys and duplicate
    # Gatekeepers. The lock is advisory and auto-releases on process exit.
    require_op_lock("collect")

    # Single-convoy guard (Q707-era, enforced by hand until now): a convoy
    # whose gatehouse worktree still hosts a LIVE Gatekeeper must never be
    # raced by a second collect — concurrent convoys race on the castle
    # promotion itself. A live convoy blocks ANY new pack, regardless of which
    # quests the candidates belong to.
    repo_root = git_ops.get_repo_root()
    live_convoy: Optional[tuple[str, str]] = None
    for wt_dir in sorted((repo_root / ".kilo" / "worktrees").glob("the-gatehouse-*")):
        m = re.match(r"the-gatehouse-(cogship-\d+)$", wt_dir.name)
        if not m or not session_is_fresh(wt_dir):
            continue
        live_convoy = (m.group(1), str(wt_dir))
        break
    if live_convoy is not None:
        print(
            f"ERROR: convoy {live_convoy[0]} is LIVE — its gatehouse worktree "
            f"({live_convoy[1]}) has a session updated in the last 30 min, meaning a Gatekeeper "
            f"is mid-integration. Concurrent convoys race on the castle promotion; wait for it "
            f"to finish (or tear it down by hand) instead of starting another.",
            file=sys.stderr,
        )
        sys.exit(1)

    # cogship-040/041 + cogship-247/248 guard: quests already stamped on a
    # prior convoy are re-packed only when that convoy is provably dead. A
    # live prior convoy (fresh Gatekeeper session in its gatehouse worktree)
    # means another operator's integration is in flight — skip, don't race it.
    requested_cogship = getattr(args, "cogship", None)
    live_skipped: list[tuple[str, str]] = []
    repackable: list = []
    for q in accepted:
        prior = store.normalize_cogship_id(q.cogship_id or "")
        if not prior or prior == store.normalize_cogship_id(requested_cogship or ""):
            repackable.append(q)
            continue
        is_live, why = gatehouse_convoy_is_live(prior, repo_root=git_ops.get_repo_root())
        if is_live:
            live_skipped.append((q.id, why))
        else:
            repackable.append(q)
    for qid, why in live_skipped:
        print(f"⏭️  Skipping {qid}: prior convoy still live — {why}")
    if live_skipped and not repackable:
        print("(all candidates belong to live prior convoys — nothing packed)")
        return
    # force=True is required whenever any candidate carries a prior stamp from
    # a dead convoy (deliberate re-pack); fresh quests take the guarded path.
    repack_force = any(q.cogship_id for q in repackable)
    try:
        cogship_id = store.stamp_cogship(
            repackable,
            cogship_id=requested_cogship,
            auto_commit=auto_commit,
            force=repack_force,
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    accepted = repackable
    print(f"🚢 Stamped {len(accepted)} Quest(s) onto {cogship_id}.")

    for quest in accepted:
        quest.set_status("GATE", f"Packed in {cogship_id}; dispatched to Gatekeeper")
    # One batched commit for the whole GATE advance (was: one commit per quest).
    store.save_many(accepted, f"court: advance {len(accepted)} quest(s) to GATE in {cogship_id}", auto_commit=auto_commit)
    for quest in accepted:
        print(f"{quest.id}: GATE")

    accepted_ids = ", ".join(q.id for q in accepted)
    gatekeeper_model = config.get_model("gatekeeper")
    qual_gatekeeper = canonical_model_id(gatekeeper_model)
    if len(accepted) == 1:
        solo_id = accepted[0].id
        solo_quest = accepted[0]
        next_steps = [
            f"Packed 1 Quest: {solo_id}",
            "",
            "Convoy size 1 -- no separate Gatehouse worktree needed. Nothing to batch,",
            "so run the Gatekeeper role directly inside this Quest's own existing",
            "worktree via Kilo CLI:",
            "",
            f"   kilo run --agent gatekeeper --model {qual_gatekeeper} --dir {solo_quest.worktree or '<worktree>'} \"Act as Gatekeeper for {solo_id}: merge castle in, run test suite, and on a clean pass merge straight into castle.\"",
            "",
            "1. Record the session:",
            f"   python3 -m court.cli set-field {solo_id} gatekeeper_session_id <session_id>",
            f"   python3 -m court.cli set-field {solo_id} gatekeeper_model \"{gatekeeper_model}\"",
            "",
            "2. On a clean suite run, promote directly into castle and advance to",
            "   READY_TO_RAZE. Run the suite via the ENGINE so it stamps proof",
            "   (the advance gate refuses without it; an agent's own shell tool",
            "   times out at 120s and cannot run a real suite):",
            f"   python3 -m court.cli runsuite {solo_id} --dir {solo_quest.worktree or '<worktree>'}",
        ]
    else:
        wt_name = f"the-gatehouse-{cogship_id}"
        branch_name = f"the-gatehouse/{cogship_id}"
        next_steps = [
            f"Packed {len(accepted)} Quest(s): {accepted_ids}",
            "",
            "Convoy size > 1 -- spawn a BRAND-NEW ephemeral Gatehouse worktree scoped to",
            "just this convoy via Kilo CLI:",
            "",
            f"1. Stand up the convoy worktree and Gatekeeper session via Kilo CLI:",
            f"   kilo worktree create {wt_name}",
            f"   git -C .kilo/worktrees/{wt_name} branch -m {branch_name}",
            f"   kilo run --agent gatekeeper --model {qual_gatekeeper} --dir .kilo/worktrees/{wt_name} \"Act as Gatekeeper for {cogship_id}: merge in branches for {accepted_ids}, run unified suite once, promote clean remainder directly into castle.\"",
            "",
            "2. Run the unified suite via the ENGINE in the convoy worktree (stamps the",
            "   proof the READY_TO_RAZE gate requires; never run suites through an",
            "   agent shell tool — its 120s timeout killed every cogship-082 run):",
            f"   python3 -m court.cli runsuite --cogship {cogship_id} --dir .kilo/worktrees/{wt_name}",
            "",
            "3. Record the session on each packed Quest:",
            f"   python3 -m court.cli set-field <id> gatekeeper_session_id <session_id>",
            f"   python3 -m court.cli set-field <id> gatekeeper_model \"{gatekeeper_model}\"",
            "",
            "4. Tear the ephemeral worktree down once promoted.",
        ]
    stood_up = False
    standup_steps: list[str] = []
    if getattr(args, "standup", False):
        kilo_bin = find_kilo_binary()
        if kilo_bin:
            if len(accepted) == 1:
                solo = accepted[0]
                if solo.gatekeeper_session_id:
                    # cogship-247/248: a re-run collect --standup spawned a
                    # duplicate Gatekeeper beside the live one. One convoy,
                    # one Gatekeeper — resume the recorded session instead.
                    print(f"⏭️  Skipping Gatekeeper standup: {solo.id} already records Gatekeeper session {solo.gatekeeper_session_id}. Resume it instead of spawning a duplicate.")
                elif solo.worktree and Path(solo.worktree).is_dir():
                    wt = Path(solo.worktree)
                    setup_worktree_agent_config(wt, "gatekeeper")
                    prompt = f"Act as Gatekeeper for {solo.id} on {cogship_id}: merge castle in, run test suite, and on clean pass merge into castle."
                    res = standup_kilo_session(wt, agent="gatekeeper", model=qual_gatekeeper, prompt=prompt, title=f"{cogship_id} Gatekeeper", kilo_bin=kilo_bin, run_now=True, watch_quest=solo.id)
                    sid = res.get("session_id")
                    if sid:
                        solo.gatekeeper_session_id = sid
                        solo.gatekeeper_model = gatekeeper_model
                        store.save(solo, auto_commit=auto_commit, commit_msg=f"court: record Gatekeeper {sid} for {solo.id}")
                        print(f"🛡️ Stood up Gatekeeper session {sid} for {solo.id} via Kilo CLI.")
                        stood_up = True
                        log_path = res.get("log") or f"{solo.worktree}/.kilo/gatekeeper.log"
                        standup_steps = [
                            f"- Gatekeeper session {sid} ({gatekeeper_model}) is integrating in",
                            f"  {solo.worktree} on branch {solo.branch or '-'}.",
                            "- Gatekeeper will merge castle in, run the unified suite via `court runsuite`, and merge into castle.",
                            f"- Worker log: tail -f {log_path}",
                            f"- Once promoted to castle and advanced to READY_TO_RAZE, raze the worktree: `court raze {solo.id}`",
                        ]
            else:
                root = git_ops.get_repo_root()
                wt_name = f"the-gatehouse-{cogship_id}"
                branch_name = f"the-gatehouse/{cogship_id}"
                wt_path = root / ".kilo" / "worktrees" / wt_name
                if wt_path.is_dir():
                    # The convoy worktree already exists: never blind-create or
                    # re-stand over it. A fresh session in it means a Gatekeeper
                    # is already integrating this convoy (cogship-247/248
                    # duplicate-Gatekeeper incident); a stale one means an
                    # earlier convoy died mid-integration and must be resumed
                    # or torn down by hand, not silently re-created.
                    if session_is_fresh(wt_path):
                        print(
                            f"ERROR: Gatehouse worktree {wt_path} already has a session updated in the "
                            f"last 30 min — a Gatekeeper is already integrating {cogship_id}. "
                            f"Do not stand up a duplicate; resume that session instead.",
                            file=sys.stderr,
                        )
                        sys.exit(1)
                    print(
                        f"ERROR: Gatehouse worktree {wt_path} already exists (no fresh session — a prior "
                        f"convoy likely died mid-integration). Refusing to re-create it: resume the "
                        f"existing worktree by hand or tear it down first: git -C {wt_path} status",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                try:
                    # Timeouts are mandatory here: `kilo worktree create` used to
                    # be the only unbounded subprocess in court, so a prompt/hang
                    # inside it blocked `collect --standup` forever.
                    subprocess.run([str(kilo_bin), "worktree", "create", wt_name], cwd=str(root), capture_output=True, check=True, timeout=300)
                    git_ops.clear_git_cache()
                    subprocess.run(["git", "-C", str(wt_path), "branch", "-m", branch_name], capture_output=True, timeout=60)
                    subprocess.run(["git", "-C", str(wt_path), "merge", base_branch, "--ff-only"], capture_output=True, timeout=120)
                    setup_worktree_agent_config(wt_path, "gatekeeper")
                    prompt = f"Act as Gatekeeper for {cogship_id}: integrate Quests {accepted_ids}, run integration suite, promote to castle."
                    res = standup_kilo_session(wt_path, agent="gatekeeper", model=qual_gatekeeper, prompt=prompt, title=f"{cogship_id} Gatekeeper", kilo_bin=kilo_bin, run_now=True, watch_quest=accepted_ids)
                    sid = res.get("session_id")
                    if sid:
                        for q in accepted:
                            q.gatekeeper_session_id = sid
                            q.gatekeeper_model = gatekeeper_model
                            store.save(q, auto_commit=auto_commit, commit_msg=f"court: record Gatekeeper {sid} for {q.id}")
                        print(f"🛡️ Stood up Gatekeeper convoy session {sid} on {branch_name} via Kilo CLI.")
                        stood_up = True
                        log_path = res.get("log") or f"{wt_path}/.kilo/gatekeeper.log"
                        standup_steps = [
                            f"- Gatekeeper session {sid} ({gatekeeper_model}) is integrating convoy {cogship_id} in",
                            f"  {wt_path} on branch {branch_name}.",
                            f"- Quests packed: {accepted_ids}",
                            "- Gatekeeper will merge candidate branches in, run the unified suite via `court runsuite`, and promote to castle.",
                            f"- Worker log: tail -f {log_path}",
                            "- Once promoted to castle and Quests advanced to READY_TO_RAZE, raze: `court raze <id>`",
                        ]
                except Exception as e:
                    print(f"⚠️ Could not auto-standup Gatekeeper: {e}")

    if stood_up:
        _print_next_steps(f"🛡️ NEXT STEPS — GATEKEEPER IS INTEGRATING {cogship_id}", standup_steps)
    else:
        _print_next_steps(f"🛡️ NEXT STEPS — SUMMON THE GATEKEEPER FOR {cogship_id}", next_steps)


def _verify_branch_exists(branch: str, repo_root: Path) -> bool:
    res = git_ops._run(["git", "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}"], repo_root)
    return bool(res.get("ok") and (res.get("stdout") or "").strip())


def _quest_worktree_charter(quest: Quest) -> Optional[Quest]:
    """Load the quest's charter from its OWN worktree when one exists.

    The chicken-and-egg collection-gate fix (studio four, Q707-era): royal
    UI-review APPROVED lines and coin verdicts are committed on the quest
    branch/worktree and cannot reach the castle baseline until a convoy
    promotes — but `collect` reads the castle ledger, so it saw stale PENDING
    forever. The anti-tampering machinery already reads the worktree charter;
    the collection gates do the same now, falling back to the castle copy
    only when no worktree exists."""
    wt = (quest.worktree or "").strip()
    if not wt:
        return None
    wt_path = Path(wt)
    if not wt_path.is_dir():
        return None
    for base in (wt_path / ".court" / "quests", wt_path / ".court" / "epics"):
        p = base / f"{quest.id}.md"
        if p.is_file():
            try:
                return Quest.from_markdown(p.read_text(encoding="utf-8"))
            except ValueError:
                return None
    return None


def _pack_quest_branches(quests: list, wt_path: Path, cogship_id: str) -> tuple[list, list]:
    """Sequentially merge candidate Quest branches into the convoy worktree.

    A branch that conflicts with earlier merges is isolated (merge aborted,
    tree restored) and reported back — it stays stamped on the Cog Ship and
    the Gatekeeper integrates it in the normal post-review pass.
    """
    merged: list = []
    isolated: list[tuple[str, str]] = []
    for quest in quests:
        res = git_ops._run(
            ["git", "merge", quest.branch, "--no-edit", "-m", f"court: pack {quest.id} into {cogship_id} (atelier)"],
            wt_path,
        )
        if res.get("ok"):
            merged.append(quest)
            continue
        git_ops._run(["git", "merge", "--abort"], wt_path)
        git_ops._run(["git", "reset", "--hard", "HEAD"], wt_path)
        reason = ((res.get("stderr") or "") + (res.get("stdout") or "")).strip().splitlines()
        isolated.append((quest.id, reason[-1][:200] if reason else "merge failed"))
    return merged, isolated


# ---------------------------------------------------------------------------
# Combined Artist Studio (Q-2): deterministic multi-quest studio command.
# Formalizes the hand-run recipe exercised on the Q472/Q473/Q412, Q589, and
# Q617 cohorts: studio worktree from castle tip, merge N quest branches under
# the established conflict policy, freshness-gated runserver, studio + session
# recorded in the ledger, and both documented spawn paths kept intact.
# ---------------------------------------------------------------------------

# Charter-paperwork paths where a studio merge conflict is ALWAYS resolved
# branch-wins: the Quest branch carries the freshest paperwork (tribute,
# audit, ledger trail) and the trunk-side copy is by definition older. Every
# other conflicted path is a genuine code overlap and goes through the
# disclosed union resolution.
STUDIO_PAPERWORK_PREFIXES = (
    ".court/quests/",
    ".court/epics/",
    ".court/archive/",
)
STUDIO_PAPERWORK_FILES = (
    ".court/LEDGER.md",
    ".court/EDICTS.md",
    ".kilo/TASK.md",
    ".kilo/TASK_ARTIST.md",
)


def _is_paperwork_path(path: str) -> bool:
    """Pure classifier: is this conflicted path charter paperwork (branch-wins)
    or genuine code overlap (disclosed union resolution)?"""
    p = (path or "").strip().strip('"').strip("'")
    return p.startswith(STUDIO_PAPERWORK_PREFIXES) or p in STUDIO_PAPERWORK_FILES


def _conflicted_paths(wt_path: Path) -> list[str]:
    res = git_ops._run(["git", "diff", "--name-only", "--diff-filter=U"], wt_path)
    if not res.get("ok"):
        return []
    return [ln.strip() for ln in (res.get("stdout") or "").splitlines() if ln.strip()]


def _checkout_theirs_path(wt_path: Path, path: str) -> bool:
    """Branch-wins resolution for one paperwork path (the Quest branch holds the
    full tribute + audit). Honors a branch-side deletion."""
    res = git_ops._run(["git", "checkout", "--theirs", "--", path], wt_path)
    if res.get("ok"):
        return bool(git_ops._run(["git", "add", "--", path], wt_path).get("ok"))
    return bool(git_ops._run(["git", "rm", "-q", "--", path], wt_path).get("ok"))


def _union_merge_path(wt_path: Path, path: str) -> tuple[bool, str]:
    """Deterministic union resolution for one genuinely code-overlapped path.

    Mechanizes the hand-run studio policy: take BOTH sides' line-level changes
    (git merge-file --union) instead of exercising aesthetic judgment, then
    guard the result — no conflict markers may remain, and .py sources must
    still byte-compile. Paths union cannot resolve (add/add, modify/delete —
    no common base to merge against) are left for isolation, never hand-merged
    here. The resolution is disclosed to the Court Artist for live sanity-check.
    """
    blobs: dict[str, Optional[str]] = {}
    for label, stage in (("base", ":1"), ("ours", ":2"), ("theirs", ":3")):
        res = git_ops._run(["git", "show", f"{stage}:{path}"], wt_path)
        blobs[label] = res.get("stdout") if res.get("ok") else None
    ours, theirs, base = blobs["ours"], blobs["theirs"], blobs["base"]
    if ours is None or theirs is None:
        return False, "modify/delete conflict — no both-side content to union"
    if ours == theirs:
        (wt_path / path).write_text(ours if ours.endswith("\n") else ours + "\n", encoding="utf-8")
        if not git_ops._run(["git", "add", "--", path], wt_path).get("ok"):
            return False, "could not stage identical content"
        return True, "both sides identical (staged as-is)"
    if base is None:
        return False, "add/add conflict — no common base for a union; needs hand resolution"
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        f_ours, f_base, f_theirs = tmpd / "ours", tmpd / "base", tmpd / "theirs"
        f_ours.write_text(ours, encoding="utf-8")
        f_base.write_text(base, encoding="utf-8")
        f_theirs.write_text(theirs, encoding="utf-8")
        res = git_ops._run(
            ["git", "merge-file", "--union", "-p", str(f_ours), str(f_base), str(f_theirs)],
            wt_path,
        )
    merged = res.get("stdout") or ""
    if not merged.strip():
        return False, f"union merge produced no output ({((res.get('stderr') or 'unknown'))[:160]})"
    if "<<<<<<<" in merged or ">>>>>>>" in merged:
        return False, "union left conflict markers; needs hand resolution"
    if path.endswith(".py"):
        try:
            compile(merged, path, "exec")
        except SyntaxError as e:
            return False, f"union broke {path} syntax ({str(e)[:120]}); needs hand resolution"
    (wt_path / path).write_text(merged if merged.endswith("\n") else merged + "\n", encoding="utf-8")
    if not git_ops._run(["git", "add", "--", path], wt_path).get("ok"):
        return False, "could not stage union result"
    return True, "union of both sides' line-level changes"


def _merge_quest_branches_with_policy(quests: list, wt_path: Path, studio_label: str) -> tuple[list, list[dict]]:
    """Sequentially merge candidate Quest branches into the studio worktree
    under the established conflict policy (Q-2; hand-run Q472/Q473/Q412, Q589,
    and Q617 cohorts):

    - charter paperwork (.court/quests/**, ledger, task files) -> branch-wins;
      the Quest branch carries the full tribute + audit, the trunk-side copy
      is older
    - genuine code overlap -> disclosed union of both sides' line-level
      changes (git merge-file --union), guarded by marker + syntax checks
    - anything the policy cannot resolve -> the branch is isolated (merge
      aborted, tree restored) and reported; it never enters the review
      half-merged

    Returns (merged_quests, per-quest reports). Each report is
    ``{"id", "paperwork": [paths], "union": ["path — note"], "isolated": reason|None}``.
    """
    merged: list = []
    reports: list[dict] = []
    for quest in quests:
        res = git_ops._run(
            [
                "git", "merge", quest.branch, "--no-edit", "-m",
                f"court: merge {quest.id} into combined artist studio {studio_label}",
            ],
            wt_path,
        )
        if res.get("ok"):
            merged.append(quest)
            reports.append({"id": quest.id, "paperwork": [], "union": [], "isolated": None})
            continue
        paths = _conflicted_paths(wt_path)
        if not paths:
            git_ops._run(["git", "merge", "--abort"], wt_path)
            git_ops._run(["git", "reset", "--hard", "HEAD"], wt_path)
            reason = ((res.get("stderr") or "") + (res.get("stdout") or "")).strip().splitlines()
            reports.append({
                "id": quest.id, "paperwork": [], "union": [],
                "isolated": reason[-1][:200] if reason else "merge failed",
            })
            continue
        paperwork: list[str] = []
        union: list[tuple[str, str]] = []
        unresolved: list[str] = []
        for p in paths:
            if _is_paperwork_path(p):
                if _checkout_theirs_path(wt_path, p):
                    paperwork.append(p)
                else:
                    unresolved.append(f"{p} (branch-wins checkout failed)")
            else:
                ok, note = _union_merge_path(wt_path, p)
                if ok:
                    union.append((p, note))
                else:
                    unresolved.append(f"{p} ({note})")
        union_str = [f"{p} — {note}" for p, note in union]
        if unresolved:
            git_ops._run(["git", "merge", "--abort"], wt_path)
            git_ops._run(["git", "reset", "--hard", "HEAD"], wt_path)
            reports.append({
                "id": quest.id, "paperwork": paperwork, "union": union_str,
                "isolated": "unresolved conflicts: " + "; ".join(unresolved),
            })
            continue
        commit_res = git_ops._run(["git", "commit", "--no-edit"], wt_path)
        if commit_res.get("ok"):
            merged.append(quest)
            reports.append({"id": quest.id, "paperwork": paperwork, "union": union_str, "isolated": None})
        else:
            git_ops._run(["git", "merge", "--abort"], wt_path)
            git_ops._run(["git", "reset", "--hard", "HEAD"], wt_path)
            reason = ((commit_res.get("stderr") or "") + (commit_res.get("stdout") or "")).strip().splitlines()
            reports.append({
                "id": quest.id, "paperwork": paperwork, "union": union_str,
                "isolated": reason[-1][:200] if reason else "commit after conflict resolution failed",
            })
    return merged, reports


def _studio_slug(quests: list) -> str:
    """Hand-run naming: `artist-studio-q617-q627-q628` / branch
    `artist/q617-q627-q628-ui-studio` — short id segments, lowercased."""
    return "-".join((q.id.split("-", 1)[0] or q.id).lower() for q in quests)


def _run_freshness_gate(repo_root: Path) -> dict:
    """24h freshness gate for the studio runserver (Q672 doctrine): WARN-ONLY.

    Runs the project-specific `studio.freshness_command` from the manifest
    (e.g. pb-app: `python3 scripts/db/local_db.py --age`, reporting the local
    production mirror's sync age). NEVER auto-syncs — a stale mirror surfaces
    as a loud warning and the review proceeds.
    """
    gate = config.get_studio_freshness()
    out: dict[str, Any] = {
        "configured": bool(gate["command"]),
        "command": gate["command"],
        "max_age_hours": gate["max_age_hours"],
        "age_hours": None,
        "fresh": None,
        "detail": "",
    }
    if not gate["command"]:
        out["detail"] = "no studio.freshness_command configured in .court/config.json"
        return out
    try:
        res = subprocess.run(
            shlex.split(gate["command"]),
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception as e:
        out["detail"] = f"freshness command failed to run ({e}) — WARN-ONLY, continuing"
        return out
    text = ((res.stdout or "") + "\n" + (res.stderr or "")).strip()
    out["detail"] = text[-600:]
    m = re.search(r"Freshness:\s*(\w+)", text)
    if m:
        out["fresh"] = m.group(1).upper() == "FRESH"
    a = re.search(r"\(([\d.]+)\s*([smhd])\s*ago\)", text)
    if a:
        mult = {"s": 1.0 / 3600.0, "m": 1.0 / 60.0, "h": 1.0, "d": 24.0}[a.group(2)]
        out["age_hours"] = round(float(a.group(1)) * mult, 2)
    if res.returncode != 0 and out["fresh"] is None:
        out["detail"] += "\n(freshness command exited non-zero — WARN-ONLY, continuing)"
    return out


def _freshness_verdict(freshness: dict) -> str:
    """One-line WARN-ONLY verdict for the studio report and ledger note."""
    if not freshness.get("configured"):
        return "freshness gate not configured (no studio.freshness_command in .court/config.json)"
    max_age = freshness.get("max_age_hours", 24)
    age = freshness.get("age_hours")
    if age is not None:
        if age > max_age:
            return (
                f"STALE WARN — mirror age {age}h exceeds the {max_age}h freshness gate; "
                "review proceeds, NEVER auto-syncs (run the configured freshness command's sync out-of-band)"
            )
        return f"OK — mirror age {age}h (gate: {max_age}h)"
    if freshness.get("fresh") is False:
        return "STALE WARN per the freshness command; review proceeds, NEVER auto-syncs"
    if freshness.get("fresh") is True:
        return "OK per the freshness command"
    return "unparsed freshness output — WARN-ONLY, review proceeds"


def _studio_sync_back(args, auto_commit: bool, repo_root: Path) -> None:
    """Post-sign-off submode: merge the studio branch back into each selected
    Quest's own worktree branch (the hand-run one-convoy sync-back pattern).

    Deliberately NOT auto-resolving here: a sync-back conflict means the Quest
    worktree drifted since the studio was cut, and the Quest branch is the
    integration target — conflicts are disclosed for a remediation turn, the
    merge is aborted, and the studio branch is never forced.
    """
    quests = resolve_quest_selection(args, batchable=True, required=True)
    studio_branch = getattr(args, "branch", None) or f"artist/{_studio_slug(quests)}-ui-studio"
    if not _verify_branch_exists(studio_branch, repo_root):
        print(
            f"ERROR: studio branch '{studio_branch}' not found in this repo. If the studio was "
            "cut with a different cohort or --branch, pass the exact studio branch.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Guarded primitive (studio_close doctrine): a drifted base replays stale
    # lines into the Quest branches, and a Quest already stamped into a live
    # convoy must never be synced behind the Gatekeeper's back.
    from . import studio_close as _sc
    drift = _sc.base_drift_count(repo_root, studio_branch, "castle")
    threshold = _sc.drift_threshold(args)
    if drift is None:
        print("ERROR: base drift unmeasurable (merge-base failed) — refusing rather than guessing. "
              "Cut a fresh studio or investigate the branch state.", file=sys.stderr)
        sys.exit(1)
    if drift > threshold and not getattr(args, "force_union", False):
        print(
            f"ERROR: base drift {drift} commits exceeds threshold {threshold} — a merge from this "
            f"cut point would replay stale deltas into the Quest branches. Re-cut the studio from "
            "the current castle tip, or pass --force-union to override.",
            file=sys.stderr,
        )
        sys.exit(1)
    if drift > threshold:
        print(f"⚠️  base drift {drift} commits exceeds threshold {threshold} — proceeding via --force-union")
    race_blocked: list[tuple[str, str]] = []
    for quest in quests:
        race = _sc.gatehouse_race(repo_root, quest)
        if race:
            race_blocked.append((quest.id, race))
    if race_blocked:
        for qid, why in race_blocked:
            print(f"ERROR: {qid}: convoy-race guard — {why}", file=sys.stderr)
        print("Refusing sync-back while these Quests sit in a live convoy lane.", file=sys.stderr)
        sys.exit(1)

    results: list[tuple[str, str, str]] = []
    changed: list[Quest] = []
    for quest in quests:
        wt = quest.worktree
        if not wt or not Path(wt).is_dir():
            results.append((quest.id, "SKIPPED", f"no Quest worktree at '{wt or '-'}'"))
            continue
        wtp = Path(wt)
        status = git_ops._run(["git", "status", "--porcelain"], wtp)
        if (status.get("stdout") or "").strip():
            results.append((quest.id, "SKIPPED", "dirty worktree — commit or stash first"))
            continue
        res = git_ops._run(
            [
                "git", "merge", studio_branch, "--no-edit", "-m",
                f"court: sync-back combined studio {studio_branch} into {quest.id}",
            ],
            wtp,
        )
        if res.get("ok"):
            results.append((quest.id, "MERGED", f"{studio_branch} merged into {quest.branch}"))
            quest.log_ledger(
                quest.status, quest.status,
                f"Synced combined studio {studio_branch} into {quest.branch} (royal sign-off complete; collection next)",
            )
            changed.append(quest)
        else:
            conflicts = _conflicted_paths(wtp)
            if conflicts:
                git_ops._run(["git", "merge", "--abort"], wtp)
                git_ops._run(["git", "reset", "--hard", "HEAD"], wtp)
                results.append((
                    quest.id, "CONFLICT",
                    f"sync-back conflicts {conflicts} — merge aborted, worktree restored; "
                    "needs a remediation turn (studio branch was NOT forced)",
                ))
            else:
                reason = ((res.get("stderr") or "") + (res.get("stdout") or "")).strip().splitlines()
                results.append((quest.id, "FAILED", reason[-1][:200] if reason else "merge failed"))

    if changed:
        store.save_many(
            changed,
            f"court: record combined-studio sync-back for {', '.join(q.id for q in changed)}",
            auto_commit=auto_commit,
        )

    print("=" * 76)
    print("🔀 COMBINED STUDIO SYNC-BACK")
    print("=" * 76)
    print(f"Studio branch: {studio_branch}")
    for qid, outcome, detail in results:
        icon = {"MERGED": "✅", "SKIPPED": "⏭️ ", "CONFLICT": "⚠️ ", "FAILED": "❌"}.get(outcome, "•")
        print(f"  {icon} {qid}: {outcome} — {detail}")
    print("=" * 76)


def cmd_atelier(args):
    """Royal UI Convoy Atelier: roll up multiple UI-review-pending Quests into one
    Cog Ship convoy PRE-integration-test, merge their branches together into the
    ephemeral gatehouse convoy worktree, and spawn ONE Court Artist session with a
    live runserver on the merged (untested) branch.

    Velocity rationale: UI changes are typically small, so reviewing each in its own
    Quest worktree burns a full session round-trip per Quest. The atelier batches
    them: branches are merged together first, M'Lord reviews all UI in one browser
    session, and (uniquely in the whole pipeline) may direct ADDITIONAL unchartered
    UI changes during the review ("Royal Addendum") — sanctioned here because the
    convoy artist template forces per-commit attribution (commit trailers +
    `## Royal Addendum` charter sections), so later MoC/Gatekeeper audits can see
    exactly why the convoy diff contains commits belonging to no single Quest branch.

    Single-writer sequencing is enforced by protocol: the artist is the sole writer
    in the convoy worktree until sign-off; only then is the Gatekeeper stood up in
    the same worktree for the unified integration suite and promotion. If the
    Gatekeeper later isolates/rejects a Quest, addendum polish entangled with that
    Quest's files is reverted alongside it (see gatekeeper_review_prompt.md).

    Selection gates mirror `collect` EXCEPT the UI Review gate is inverted: only
    Quests whose UI Review is PENDING are accepted — already-approved or
    headless Quests have nothing for the artist and route through /collect.
    """
    base_branch = getattr(args, "base", "castle") or "castle"
    auto_commit = not getattr(args, "no_commit", False)
    candidates = resolve_quest_selection(args, batchable=True, required=True, default_status="TRIBUTE_READY,GATE")
    repo_root = git_ops.get_repo_root()

    accepted: list[Quest] = []
    skipped: list[tuple[str, str]] = []
    scan_warnings: list[str] = []
    for quest in candidates:
        if quest.status == "PUNISHED":
            skipped.append((quest.id, "PUNISHED (side-state, frozen pending its pillory successor)"))
            continue
        if quest.status not in ("TRIBUTE_READY", "GATE"):
            skipped.append((quest.id, f"status is [{quest.status}], not TRIBUTE_READY or GATE"))
            continue
        wt_quest = _quest_worktree_charter(quest)
        effective_quest = wt_quest if wt_quest is not None else quest
        if not (effective_quest.body_sections.get("Master of Coin's Audit", "").strip() or quest.body_sections.get("Master of Coin's Audit", "").strip()):
            skipped.append((
                quest.id,
                "no recorded Master of Coin's Audit content -- refusing to pack unaudited "
                "tribute into a convoy (dispatch `/levy <id>` first)",
            ))
            continue
        ui_status = effective_quest.extract_ui_review_status() or quest.extract_ui_review_status()
        if not ui_status.upper().startswith("PENDING"):
            skipped.append((
                quest.id,
                f"UI Review is not PENDING ('{ui_status or 'not recorded'}') -- nothing for the "
                "artist to review; route through /collect instead",
            ))
            continue
        if not quest.branch or not _verify_branch_exists(quest.branch, repo_root):
            skipped.append((quest.id, f"branch '{quest.branch or '-'}' not found in this repo -- nothing to merge"))
            continue
        audit = ward.audit_quest(quest, base_branch=base_branch)
        if audit.git_status.get("dirty"):
            skipped.append((quest.id, "dirty working tree"))
            continue
        if audit.violations:
            skipped.append((quest.id, f"{len(audit.violations)} compliance violation(s): {'; '.join(audit.violations)}"))
            continue
        try:
            contraband = migration_guard.scan_branch_contraband(repo_root, base_branch, quest.branch)
        except Exception as e:
            scan_warnings.append(f"{quest.id}: migration contraband scan failed ({e}); Gatekeeper graph check remains the backstop")
        else:
            if contraband:
                skipped.append((quest.id, migration_guard.remediation_message(contraband, base_branch)))
                continue
        accepted.append(quest)

    if skipped:
        print(f"⏭️  Skipped {len(skipped)} candidate(s) (not atelier material):")
        for qid, reason in skipped:
            print(f"   - {qid}: {reason}")
    for warning in scan_warnings:
        print(f"⚠️  {warning}")

    if not accepted:
        print("(no UI-review-pending Quests ready to roll up into an atelier convoy)")
        return

    if len(accepted) == 1:
        print(f"ℹ️  Only one UI Quest accepted ({accepted[0].id}) — for a single Quest the lighter "
              f"per-Quest review is `/artist {accepted[0].id}` (no convoy worktree needed). "
              "Proceeding with the atelier anyway for the Royal Addendum protocol.")

    # Atelier is a deliberate single-operator batch flow (M'Lord drives it
    # interactively); re-rolls of already-stamped UI quests are intentional,
    # so the duplicate-convoy backstop is overridden here.
    cogship_id = store.stamp_cogship(accepted, cogship_id=getattr(args, "cogship", None), auto_commit=auto_commit, force=True)
    print(f"🎨 Stamped {len(accepted)} UI Quest(s) onto {cogship_id} for the Royal Atelier (pre-integration review).")

    for quest in accepted:
        quest.set_status("GATE", f"Packed in {cogship_id}; routed to the Royal Atelier (convoy UI review before integration tests)")
    store.save_many(accepted, f"court: advance {len(accepted)} quest(s) to GATE in {cogship_id} (atelier)", auto_commit=auto_commit)
    for quest in accepted:
        print(f"{quest.id}: GATE (atelier)")

    # Ephemeral convoy worktree — same shape as a collect standup convoy, but it is
    # MANDATORY here: the entire point is reviewing the merged (untested) branch.
    wt_name = f"the-gatehouse-{cogship_id}"
    branch_name = f"the-gatehouse/{cogship_id}"
    wt_path = repo_root / ".kilo" / "worktrees" / wt_name
    if wt_path.exists():
        print(
            f"ERROR: convoy worktree already exists at {wt_path} — an atelier/convoy is already "
            f"stood up for {cogship_id}. Single-writer rule: tear the existing worktree down or "
            "pass a different --cogship.",
            file=sys.stderr,
        )
        sys.exit(1)

    kilo_bin = find_kilo_binary()
    created = False
    if kilo_bin:
        try:
            subprocess.run(
                [str(kilo_bin), "worktree", "create", wt_name],
                cwd=str(repo_root), capture_output=True, check=True, timeout=300,
            )
            git_ops.clear_git_cache()
            subprocess.run(["git", "-C", str(wt_path), "branch", "-m", branch_name], capture_output=True, timeout=60)
            created = True
        except Exception as e:
            print(f"⚠️ kilo worktree create failed ({e}); falling back to git worktree add")
    if not created:
        res = git_ops._run(["git", "worktree", "add", str(wt_path), "-b", branch_name, base_branch], repo_root)
        if not res.get("ok"):
            print(f"ERROR: could not create convoy worktree at {wt_path}: {res.get('stderr')}", file=sys.stderr)
            sys.exit(1)

    ff_res = git_ops._run(["git", "merge", base_branch, "--ff-only"], wt_path)
    if not ff_res.get("ok"):
        print(f"ERROR: convoy worktree could not fast-forward to {base_branch}: {ff_res.get('stderr')}", file=sys.stderr)
        sys.exit(1)

    merged_quests, isolated = _pack_quest_branches(accepted, wt_path, cogship_id)
    for qid, reason in isolated:
        print(f"⚠️  Isolated {qid}: branch merge conflict in convoy ({reason}). It stays stamped on "
              f"{cogship_id}; the Gatekeeper integrates it in the normal post-review pass.")
    if not merged_quests:
        print("ERROR: every candidate branch failed to merge into the convoy — no merged state to review.", file=sys.stderr)
        sys.exit(1)

    setup_worktree_agent_config(wt_path, "artist")
    port, runserver_url, server_status = _ensure_worktree_server(
        wt_path,
        port_override=getattr(args, "port", None),
        no_server=getattr(args, "no_server", False),
    )

    model = getattr(args, "model", None) or config.get_model("artist")
    provider = getattr(args, "provider", None) or config.get_provider("artist")

    quest_blocks: list[str] = []
    routes_by_id: dict[str, list[str]] = {}
    for quest in merged_quests:
        routes = _extract_target_routes(quest, worktree=wt_path)
        routes_by_id[quest.id] = routes
        route_str = "\n".join(f"  - {runserver_url}{r}" if not r.startswith("http") else f"  - {r}" for r in routes)
        quest_blocks.append(
            f"### {quest.id} — {quest.title}\n- Branch: `{quest.branch}`\n- Preview routes:\n{route_str}"
        )

    tmpl_path = REPO_ROOT / ATELIER_DISPATCH_TEMPLATE
    if not tmpl_path.exists():
        tmpl_path = Path(__file__).resolve().parent / "templates" / "court_artist_convoy_prompt.md"
    if tmpl_path.exists():
        tmpl_text = tmpl_path.read_text(encoding="utf-8")
    else:
        tmpl_text = (
            "You are the Court Artist for atelier {{ cogship_id }}. Worktree: {{ worktree }}. "
            "Runserver: {{ runserver_url }}. Quests: {{ quest_ids }}"
        )

    prompt = (
        tmpl_text
        .replace("{{ cogship_id }}", cogship_id)
        .replace("{{ worktree }}", str(wt_path))
        .replace("{{ branch }}", branch_name)
        .replace("{{ runserver_url }}", runserver_url)
        .replace("{{ port }}", str(port))
        .replace("{{ target_routes }}", "\n".join(
            f"- {runserver_url}{r}" if not r.startswith("http") else f"- {r}"
            for r in [x for routes in routes_by_id.values() for x in routes]
        ))
        .replace("{{ quest_blocks }}", "\n\n".join(quest_blocks))
        .replace("{{ quest_ids }}", ", ".join(q.id for q in accepted))
    )

    for quest in accepted:
        quest.artist_model = model
        note = f"Routed to the Royal Atelier ({cogship_id}) for convoy UI review with model {model} (runserver port {port})"
        if qid_reason := next((r for i, r in isolated if i == quest.id), None):
            note += f"; branch isolated from convoy merge ({qid_reason})"
        quest.log_ledger("GATE", "GATE", note)
    store.save_many(accepted, f"court: record atelier routing for {cogship_id}", auto_commit=auto_commit)

    # Write the artist brief into the convoy worktree so the single unified
    # spawn path (`kilo run ... "$(cat ...)"`) carries it verbatim.
    atelier_brief_path = wt_path / ".kilo" / "TASK_ARTIST.md"
    try:
        atelier_brief_path.parent.mkdir(parents=True, exist_ok=True)
        atelier_brief_path.write_text(prompt, encoding="utf-8")
    except Exception as e:
        atelier_brief_path = None
        print(f"⚠️  could not write {atelier_brief_path} ({e}); use --prompt-only for the brief")

    if getattr(args, "prompt_only", False):
        print(prompt)
        return

    if getattr(args, "json", False):
        out = {
            "cogship_id": cogship_id,
            "quests": [
                {"id": q.id, "title": q.title, "branch": q.branch, "routes": routes_by_id.get(q.id, [])}
                for q in accepted
            ],
            "isolated": [{"id": qid, "reason": reason} for qid, reason in isolated],
            "worktree": str(wt_path),
            "branch": branch_name,
            "port": port,
            "runserver_url": runserver_url,
            "model": model,
            "provider": provider,
            "prompt": prompt,
            "spawn": {
                "argv": [
                    "kilo", "run",
                    "--agent", "artist",
                    "--model", model,
                    "--dir", str(wt_path),
                    *([f"$(cat {atelier_brief_path})"] if atelier_brief_path else [prompt]),
                ],
                "interactive": f"cd {wt_path} && kilo   # default_agent already set to artist",
                "model": model,
                "provider": provider,
            },
        }
        print(json.dumps(out, indent=2))
        return

    short_id = cogship_id.removeprefix("cogship-")
    merged_ids = ", ".join(q.id for q in merged_quests)
    print("=" * 76)
    print("🎨 ROYAL UI ATELIER STOOD UP — BATCHED CONVOY UI REVIEW")
    print("=" * 76)
    print(f"Cog Ship:    {cogship_id}")
    print(f"Quests:      {merged_ids}" + (f"  (isolated: {', '.join(qid for qid, _ in isolated)})" if isolated else ""))
    print(f"Branch:      {branch_name}")
    print(f"Worktree:    {wt_path}")
    print(f"Runserver:   {runserver_url} (Port {port}) [{server_status}]")
    print(f"Model:       {model} ({provider})")
    print()
    print("Live Preview URLs:")
    for r in [x for routes in routes_by_id.values() for x in routes]:
        url_line = f"{runserver_url}{r}" if not r.startswith("http") else r
        print(f"  • {url_line}")
    print()
    print("Next Steps for M'Lord & Steward:")
    print("  1. Launch the Court Artist session in the convoy worktree (it is pre-configured")
    print("     as the sole writer there) via Kilo CLI — the single unified path; Agent")
    print("     Manager prompting is retired:")
    if atelier_brief_path:
        print(f"     kilo run --agent artist --model \"{model}\" --dir {wt_path} \"$(cat {atelier_brief_path})\"")
    else:
        print(f"     kilo run --agent artist --model \"{model}\" --dir {wt_path} \"<the brief>\"")
    print(f"     (or interactive: cd {wt_path} && kilo — default_agent already set to artist).")
    print(f"     (Slash command: `/atelier {merged_ids}` drives this whole flow.)")
    print(f"  2. Record the session on each packed Quest: python3 -m court.cli set-field <id> artist_session_id <session_id>")
    print(f"  3. Open the live preview: {runserver_url} — review every Quest's UI in one session.")
    print("  4. Direct any extra UI changes you want — the artist attributes them per the Royal")
    print("     Addendum protocol (commit trailers + `## Royal Addendum` charter sections).")
    print()
    print("🛡️ AFTER THE ROYAL SIGN-OFF (single-writer: the Gatekeeper enters only then):")
    print(f"  5. Run the unified integration suite via the ENGINE in the convoy worktree:")
    print(f"     python3 -m court.cli runsuite --cogship {cogship_id} --dir {wt_path}")
    print(f"  6. Hand the worktree to the Gatekeeper (candidate branches are already merged):")
    print(f"     kilo run --agent gatekeeper --model \"$(python3 -m court.cli model gatekeeper)\" --dir {wt_path}")
    print(f"     \"Act as Gatekeeper for {cogship_id}: candidate branches already merged; run the unified")
    print(f"     suite via court runsuite, promote the clean convoy into castle, advance passing Quests")
    print(f"     to READY_TO_RAZE, and pack the manifest with court ship.\"")
    print("=" * 76)


# Shared-studio MCP wiring verdict (probe 2026-09-27, /var/folders/.../kilo/mcp-probe):
# a `.kilo/kilo.json` containing only an mcp block IS sufficient — Kilo loads it
# even with an empty global config: variant A (.kilo/kilo.json) reported
# "MCP server: chrome-devtools" and listed the shared browser's tabs; the
# no-config control reported "MCP servers: None". So studio worktrees get the
# chrome-devtools MCP pre-configured in their untracked .kilo/kilo.json.
STUDIO_MCP_WORKTREE_CONFIG = True


def render_browser_note(
    wt_path: Path,
    browser_info: Optional[dict[str, Any]],
    mcp_wired: bool,
) -> str:
    """Pure renderer for the studio brief's shared-browser note.

    Independent of quests and the running browser so it stays unit-testable.
    """
    port = (browser_info or {}).get("port") or studio_browser.DEFAULT_PORT
    cdp_url = (browser_info or {}).get("cdp_url") or f"http://127.0.0.1:{port}"
    annotations_file = Path(wt_path) / ".kilo" / "studio-annotations.jsonl"

    if not browser_info:
        manual = f"npx chrome-devtools-mcp@latest --browserUrl http://127.0.0.1:{studio_browser.DEFAULT_PORT}"
        return (
            "- Shared studio browser: not running (start it with "
            f"`python3 -m court.cli browser start --annotate {wt_path}`).\n"
            f"- To give the browser tools to this session, attach the MCP manually: `{manual}`.\n"
            f"- Annotation notes file: `{annotations_file}` (append-only JSONL)."
        )

    if mcp_wired:
        mcp_line = (
            f"- Shared studio browser: the chrome-devtools MCP is pre-configured in this worktree "
            f"(`.kilo/kilo.json`); its tools connect automatically to the shared browser at `{cdp_url}`."
        )
    else:
        mcp_line = (
            f"- Shared studio browser: attach its tools manually with "
            f"`npx chrome-devtools-mcp@latest --browserUrl {cdp_url}` (CDP port {port})."
        )
    return (
        mcp_line + "\n"
        f"- M'Lord's annotation notes land in `{annotations_file}` (append-only JSONL) — "
        "read it before and during the review."
    )


def cmd_studio(args):
    """Deterministic Multi-Quest Combined Studio (Q-2): formalize the hand-run
    artist-studio recipe (Q472/Q473/Q412, Q589, Q617 cohorts) into one command.

    Cuts an `artist-studio-<ids>` worktree from the castle tip on branch
    `artist/<ids>-ui-studio`, merges every candidate Quest branch with the
    established conflict policy (charter paperwork -> branch-wins; genuine
    code overlap -> disclosed union resolution; unresolvable -> isolation),
    copies the gitignored .env into the fresh worktree, starts the runserver
    behind the WARN-ONLY freshness gate, renders the artist brief to
    `.kilo/TASK_ARTIST.md`, and records the studio + session in each Quest's
    Castle Ledger (`artist_session_id` / `artist_model`) — with a single
    unified spawn path: Kilo CLI prompting (`--standup` lets this command
    spawn and record the session; the printed `kilo run` line is the manual
    equivalent). Agent Manager prompting is retired (2026-09-28): it defaults
    sessions to steward mode, lacks agent parameterization, and its launcher
    is flaky (see LEDGER).

    Unlike /atelier, the studio does NOT stamp a Cog Ship or advance statuses:
    Quests keep their pipeline place and sync-back happens per Quest
    (`court studio <ids> --sync-back`), with collection remaining the
    Steward's. Single-writer: the studio belongs to the Court Artist until
    royal sign-off.
    """
    auto_commit = not getattr(args, "no_commit", False)
    repo_root = git_ops.get_repo_root()

    if getattr(args, "close", False):
        sys.exit(studio_close.run_close(args))

    if getattr(args, "sync_back", False):
        return _studio_sync_back(args, auto_commit, repo_root)

    base_branch = getattr(args, "base", "castle") or "castle"
    candidates = resolve_quest_selection(
        args, batchable=True, required=True, default_status="TRIBUTE_READY,GATE",
    )

    accepted: list[Quest] = []
    skipped: list[tuple[str, str]] = []
    any_status = bool(getattr(args, "any_status", False))
    for quest in candidates:
        if quest.status == "PUNISHED":
            skipped.append((quest.id, "PUNISHED (side-state, frozen pending its pillory successor)"))
            continue
        if quest.status not in ("TRIBUTE_READY", "GATE") and not any_status:
            skipped.append((quest.id, f"status is [{quest.status}], not TRIBUTE_READY or GATE (use --any-status to override)"))
            continue
        wt_quest = _quest_worktree_charter(quest)
        effective_quest = wt_quest if wt_quest is not None else quest
        if not (effective_quest.body_sections.get("Master of Coin's Audit", "").strip() or quest.body_sections.get("Master of Coin's Audit", "").strip()):
            skipped.append((
                quest.id,
                "no recorded Master of Coin's Audit content -- refusing to merge unaudited "
                "work into a royal studio (dispatch `/levy <id>` first)",
            ))
            continue
        ui_status = effective_quest.extract_ui_review_status() or quest.extract_ui_review_status()
        if not ui_status.upper().startswith("PENDING"):
            skipped.append((
                quest.id,
                f"UI Review is not PENDING ('{ui_status or 'not recorded'}') -- nothing for the "
                "artist to review; route through /collect instead",
            ))
            continue
        if not quest.branch or not _verify_branch_exists(quest.branch, repo_root):
            skipped.append((quest.id, f"branch '{quest.branch or '-'}' not found in this repo -- nothing to merge"))
            continue
        audit = ward.audit_quest(quest, base_branch=base_branch)
        if audit.git_status.get("dirty"):
            skipped.append((quest.id, "dirty working tree"))
            continue
        if audit.violations:
            skipped.append((quest.id, f"{len(audit.violations)} compliance violation(s): {'; '.join(audit.violations)}"))
            continue
        try:
            contraband = migration_guard.scan_branch_contraband(repo_root, base_branch, quest.branch)
        except Exception as e:
            print(f"⚠️  {quest.id}: migration contraband scan failed ({e}); Gatekeeper graph check remains the backstop")
        else:
            if contraband:
                skipped.append((quest.id, migration_guard.remediation_message(contraband, base_branch)))
                continue
        accepted.append(quest)

    as_json = bool(getattr(args, "json", False))
    if skipped and not as_json:
        print(f"⏭️  Skipped {len(skipped)} candidate(s) (not studio material):")
        for qid, reason in skipped:
            print(f"   - {qid}: {reason}")

    if not accepted:
        print("(no UI-review-pending Quests ready to combine into a studio)")
        return

    if len(accepted) == 1 and not as_json:
        print(f"ℹ️  Only one Quest accepted ({accepted[0].id}) — for a single Quest the lighter "
              f"per-Quest review is `/artist {accepted[0].id}` (no studio worktree needed). "
              "Proceeding with the combined studio anyway.")

    slug = _studio_slug(accepted)
    wt_name = f"artist-studio-{slug}"
    branch_name = getattr(args, "branch", None) or f"artist/{slug}-ui-studio"
    if not branch_name.startswith("artist/"):
        print(f"ERROR: --branch must use the artist/ folder namespace (got '{branch_name}').", file=sys.stderr)
        sys.exit(1)
    wt_path = repo_root / ".kilo" / "worktrees" / wt_name
    if wt_path.exists():
        print(
            f"ERROR: studio worktree already exists at {wt_path} — a combined studio is already "
            f"stood up for this cohort. Single-writer rule: tear the existing worktree down or "
            "pass --branch to cut a different studio.",
            file=sys.stderr,
        )
        sys.exit(1)
    if _verify_branch_exists(branch_name, repo_root):
        print(
            f"ERROR: branch '{branch_name}' already exists — a studio for this cohort was already "
            "cut. Pass --branch to name a fresh studio branch.",
            file=sys.stderr,
        )
        sys.exit(1)

    kilo_bin = find_kilo_binary()
    created = False
    if kilo_bin:
        try:
            subprocess.run(
                [str(kilo_bin), "worktree", "create", wt_name],
                cwd=str(repo_root), capture_output=True, check=True, timeout=300,
            )
            git_ops.clear_git_cache()
            subprocess.run(["git", "-C", str(wt_path), "branch", "-m", branch_name], capture_output=True, timeout=60)
            created = True
        except Exception as e:
            print(f"⚠️ kilo worktree create failed ({e}); falling back to git worktree add")
    if not created:
        res = git_ops._run(["git", "worktree", "add", str(wt_path), "-b", branch_name, base_branch], repo_root)
        if not res.get("ok"):
            print(f"ERROR: could not create studio worktree at {wt_path}: {res.get('stderr')}", file=sys.stderr)
            sys.exit(1)

    ff_res = git_ops._run(["git", "merge", base_branch, "--ff-only"], wt_path)
    if not ff_res.get("ok"):
        print(f"ERROR: studio worktree could not fast-forward to {base_branch}: {ff_res.get('stderr')}", file=sys.stderr)
        sys.exit(1)

    merged_quests, reports = _merge_quest_branches_with_policy(accepted, wt_path, slug)
    isolated = [(r["id"], r["isolated"]) for r in reports if r["isolated"]]
    if not as_json:
        for r in reports:
            if r["paperwork"]:
                print(f"📄 {r['id']}: charter paperwork conflicts resolved branch-wins "
                      f"(branch holds the full tribute + audit): {', '.join(r['paperwork'])}")
            for u in r["union"]:
                print(f"🔀 {r['id']}: code overlap union-resolved and disclosed to the Artist: {u}")
        for qid, reason in isolated:
            print(f"⚠️  Isolated {qid}: {reason}. It stays OUT of this studio — review it per-Quest "
                  f"(/artist {qid}) or let a later convoy integrate it after remediation.")
    if not merged_quests:
        print("ERROR: every candidate branch failed to merge into the studio — no merged state to review.", file=sys.stderr)
        sys.exit(1)

    setup_worktree_agent_config(wt_path, "artist")

    # .env plumbing — the gitignored per-checkout env file is absent in fresh worktrees
    # (the Q617 studio's first boot failed on the missing QSTASH_URL before this copy).
    env_note = ""
    root_env = repo_root / ".env"
    if root_env.is_file() and not (wt_path / ".env").exists():
        shutil.copy2(root_env, wt_path / ".env")
        env_note = ".env copied from the castle root (gitignored per-checkout file)"

    no_server = getattr(args, "no_server", False)
    port, runserver_url, server_status = _ensure_worktree_server(
        wt_path,
        port_override=getattr(args, "port", None),
        no_server=no_server,
    )
    freshness = _run_freshness_gate(repo_root) if not no_server else {
        "configured": False, "command": "", "max_age_hours": 24,
        "age_hours": None, "fresh": None, "detail": "server skipped (--no-server)",
    }
    fresh_verdict = _freshness_verdict(freshness)

    model = getattr(args, "model", None) or config.get_model("artist")
    provider = getattr(args, "provider", None) or config.get_provider("artist")

    # Shared studio browser + annotation overlay. Best-effort: the studio must
    # not die because the browser failed to start.
    browser_info: Optional[dict[str, Any]] = None
    browser_wired_mcp = False
    if not getattr(args, "no_browser", False):
        try:
            browser_info = studio_browser.start_browser(annotate_worktree=wt_path)
            try:
                (wt_path / ".worktree-browser").write_text(
                    json.dumps(
                        {
                            "port": browser_info.get("port"),
                            "cdp_url": browser_info.get("cdp_url"),
                            "profile": browser_info.get("profile"),
                            "annotator": True,
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            except Exception:
                pass
        except Exception as e:
            print(f"⚠️  shared studio browser not started ({e}); the Artist brief carries manual attach instructions")
            browser_info = None
        if browser_info and STUDIO_MCP_WORKTREE_CONFIG:
            browser_wired_mcp = ensure_worktree_browser_mcp(wt_path, int(browser_info.get("port") or 0))
    browser_mcp_line = render_browser_note(wt_path, browser_info, browser_wired_mcp)

    quest_blocks: list[str] = []
    routes_by_id: dict[str, list[str]] = {}
    for quest in merged_quests:
        routes = _extract_target_routes(quest, worktree=wt_path)
        routes_by_id[quest.id] = routes
        route_str = "\n".join(f"  - {runserver_url}{r}" if not r.startswith("http") else f"  - {r}" for r in routes)
        quest_blocks.append(
            f"### {quest.id} — {quest.title}\n- Branch: `{quest.branch}`\n"
            f"- Charter: `.court/quests/{quest.id}.md` (merged into this worktree)\n"
            f"- Preview routes:\n{route_str}"
        )

    merge_disclosure_lines: list[str] = []
    for r in reports:
        if r["paperwork"] or r["union"] or r["isolated"]:
            bits = []
            if r["paperwork"]:
                bits.append("paperwork branch-wins: " + ", ".join(r["paperwork"]))
            if r["union"]:
                bits.append("code union (verify live): " + "; ".join(r["union"]))
            if r["isolated"]:
                bits.append(f"ISOLATED (not in this studio): {r['isolated']}")
            merge_disclosure_lines.append(f"- **{r['id']}**: " + " | ".join(bits))

    sync_back_commands = "\n".join(
        f"git -C {q.worktree} merge {branch_name} --no-edit -m \"court: sync-back combined studio {slug} into {q.id}\""
        if q.worktree else
        f"# {q.id}: locate its worktree (court show {q.id}), then: git -C <worktree> merge {branch_name} --no-edit"
        for q in merged_quests
    )
    sync_back_commands += (
        f"\n# or, deterministic equivalent: python3 -m court.cli studio "
        f"{','.join(q.id for q in merged_quests)} --sync-back --branch {branch_name}"
    )

    tmpl_path = REPO_ROOT / STUDIO_DISPATCH_TEMPLATE
    if not tmpl_path.exists():
        tmpl_path = Path(__file__).resolve().parent / "templates" / "court_artist_studio_prompt.md"
    if tmpl_path.exists():
        tmpl_text = tmpl_path.read_text(encoding="utf-8")
    else:
        tmpl_text = (
            "You are the Court Artist for combined studio {{ studio_slug }}. Worktree: {{ worktree }}. "
            "Runserver: {{ runserver_url }}. Quests: {{ quest_ids }}"
        )

    prompt = (
        tmpl_text
        .replace("{{ studio_slug }}", slug)
        .replace("{{ worktree }}", str(wt_path))
        .replace("{{ branch }}", branch_name)
        .replace("{{ base_branch }}", base_branch)
        .replace("{{ runserver_url }}", runserver_url)
        .replace("{{ port }}", str(port))
        .replace("{{ model }}", model)
        .replace("{{ freshness_note }}", fresh_verdict)
        .replace("{{ browser_mcp_line }}", browser_mcp_line)
        .replace("{{ annotations_file }}", str(wt_path / ".kilo" / "studio-annotations.jsonl"))
        .replace("{{ vision_model }}", config.get_model("artist_vision", default=model))
        .replace("{{ merge_disclosures }}", "\n".join(merge_disclosure_lines) or "- (clean merges — no policy resolutions needed)")
        .replace("{{ sync_back_commands }}", sync_back_commands)
        .replace("{{ target_routes }}", "\n".join(
            f"- {runserver_url}{r}" if not r.startswith("http") else f"- {r}"
            for r in [x for routes in routes_by_id.values() for x in routes]
        ))
        .replace("{{ quest_blocks }}", "\n\n".join(quest_blocks))
        .replace("{{ quest_ids }}", ", ".join(q.id for q in merged_quests))
    )

    brief_path = wt_path / ".kilo" / "TASK_ARTIST.md"
    try:
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(prompt, encoding="utf-8")
    except Exception as e:
        print(f"⚠️  could not write {brief_path} ({e}); the rendered brief is in the command output")

    ledger_note_tail = (
        f"runserver port {port} ({fresh_verdict.split(' — ')[0]})"
        if not no_server else "runserver skipped (--no-server)"
    )
    for quest in merged_quests:
        quest.artist_model = model
        bits = [f"Routed to combined artist studio {slug} (worktree {wt_name}, branch {branch_name})"]
        r = next(x for x in reports if x["id"] == quest.id)
        if r["paperwork"]:
            bits.append(f"conflicts resolved branch-wins: {', '.join(r['paperwork'])}")
        if r["union"]:
            bits.append(f"code overlaps union-resolved: {len(r['union'])} (disclosed to Artist)")
        bits.append(ledger_note_tail)
        if env_note:
            bits.append(env_note)
        quest.log_ledger(quest.status, quest.status, "; ".join(bits))
    store.save_many(
        merged_quests,
        f"court: record combined studio {slug} routing ({len(merged_quests)} quest(s))",
        auto_commit=auto_commit,
    )

    if getattr(args, "prompt_only", False):
        print(prompt)
        return

    if getattr(args, "json", False):
        out = {
            "studio": slug,
            "quests": [
                {"id": q.id, "title": q.title, "branch": q.branch, "routes": routes_by_id.get(q.id, [])}
                for q in merged_quests
            ],
            "isolated": [{"id": qid, "reason": reason} for qid, reason in isolated],
            "conflict_policy": {
                r["id"]: {
                    "paperwork_branch_wins": r["paperwork"],
                    "union_resolved": r["union"],
                    "isolated": r["isolated"],
                }
                for r in reports
            },
            "worktree": str(wt_path),
            "branch": branch_name,
            "base": base_branch,
            "port": port,
            "runserver_url": runserver_url,
            "freshness": {"verdict": fresh_verdict, "age_hours": freshness.get("age_hours"),
                          "command": freshness.get("command"), "detail": freshness.get("detail")},
            "model": model,
            "provider": provider,
            "brief_path": str(brief_path),
            "prompt": prompt,
            "spawn": {
                "argv": [
                    "kilo", "run",
                    "--agent", "artist",
                    "--model", model,
                    "--dir", str(wt_path),
                    f"$(cat {brief_path})",
                ],
                "interactive": f"cd {wt_path} && kilo   # default_agent already set to artist",
                "model": model,
                "provider": provider,
            },
        }
        print(json.dumps(out, indent=2))
        return

    if getattr(args, "standup", False):
        spawn = standup_kilo_session(
            wt_path,
            agent="artist",
            model=model,
            prompt=prompt,
            title=f"{slug} Combined Studio",
            kilo_bin=kilo_bin,
            provider_hint="artist",
        )
        session_id = spawn.get("session_id") or ""
        # Record only REAL sessions (api/cli); the config-mode placeholder id
        # ("kilo-artist", worktree merely pre-configured) is not a session.
        if session_id and spawn.get("mode") in ("api", "cli"):
            for quest in merged_quests:
                quest.artist_session_id = session_id
            store.save_many(
                merged_quests,
                f"court: record combined studio {slug} artist session {session_id}",
                auto_commit=auto_commit,
            )
        print(f"🎨 Branch B spawn: {spawn.get('message')}")

    merged_ids = ", ".join(q.id for q in merged_quests)
    print("=" * 76)
    print("🎨 COMBINED ARTIST STUDIO STOOD UP — DETERMINISTIC MULTI-QUEST REVIEW")
    print("=" * 76)
    print(f"Studio:      {slug}")
    print(f"Quests:      {merged_ids}" + (f"  (isolated: {', '.join(qid for qid, _ in isolated)})" if isolated else ""))
    print(f"Branch:      {branch_name} (cut from {base_branch})")
    print(f"Worktree:    {wt_path}")
    print(f"Runserver:   {runserver_url} (Port {port}) [{server_status}]")
    print(f"Freshness:   {fresh_verdict}")
    print(f"Model:       {model} ({provider})")
    _browser_lines = browser_mcp_line.splitlines() or ["not configured"]
    print(f"Browser:     {_browser_lines[0].lstrip('- ')}")
    for _bline in _browser_lines[1:]:
        print(f"             {_bline.lstrip('- ')}")
    if env_note:
        print(f"Env:         {env_note}")
    print()
    print("Live Preview URLs:")
    for r in [x for routes in routes_by_id.values() for x in routes]:
        url_line = f"{runserver_url}{r}" if not r.startswith("http") else r
        print(f"  • {url_line}")
    print()
    print("Next Steps for M'Lord & Steward (single unified spawn path — prompting):")
    print("  Spawn the Court Artist session via Kilo CLI (Agent Manager prompting is")
    print("  retired: it defaults sessions to steward mode and lacks agent parameterization):")
    print(f"    kilo run --agent artist --model \"$(python3 -m court.cli model artist)\" --dir {wt_path} \"$(cat {brief_path})\"")
    print("    (or `--standup` next time to let this command spawn and record it), or interactive:")
    print(f"    cd {wt_path} && kilo   # default_agent already set to artist")
    print("    then record the session: python3 -m court.cli set-field <id> artist_session_id <session_id>")
    print(f"  Brief:    {brief_path} (also rendered above with --prompt-only)")
    print()
    print("🛡️ AFTER THE ROYAL SIGN-OFF (single-writer — no Gatekeeper in this worktree):")
    print(f"  1. Sync the studio back per Quest: python3 -m court.cli studio {merged_ids} --sync-back")
    print(f"     --branch {branch_name}")
    print("  2. Collection remains the Steward's: levy/collect the signed-off Quests into a")
    print("     Cog Ship as usual — the studio branch itself is never promoted directly.")
    print("=" * 76)


def cmd_stamp(args):
    """Stamp a batch of Quests onto one Cog Ship convoy id (cogship-NNN).

    The `--cogship` filters on `ship`/`rollup` were previously unreachable in
    practice: `store.stamp_cogship()`/`next_cogship_id()` existed but no CLI
    verb exposed them, so cogship_id could only be set via raw `set-field`.
    """
    ids = [s.strip() for s in args.quest_ids.split(",") if s.strip()]
    if not ids:
        print("ERROR: no quest ids given", file=sys.stderr)
        sys.exit(1)
    quests = []
    for qid in ids:
        try:
            quests.append(store.load(qid))
        except Exception as e:
            print(f"ERROR: {qid}: {e}", file=sys.stderr)
            sys.exit(1)
    cogship_arg = getattr(args, "cogship", None)
    cogship_id = None if (not cogship_arg or cogship_arg.lower() == "new") else cogship_arg
    auto_commit = not getattr(args, "no_commit", False)
    try:
        stamped = store.stamp_cogship(quests, cogship_id=cogship_id, auto_commit=auto_commit, force=getattr(args, "force", False))
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"🚢 Stamped {len(quests)} Quest(s) onto {stamped}:")
    for q in quests:
        print(f"   * {q.id} [{status_label(q.status)}]")


def cmd_timber(args):
    import json
    am_path = agent_manager_json_path()
    am_data = {}
    if am_path.exists():
        try:
            am_data = json.loads(am_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    am_wts = am_data.get("worktrees", {})
    am_sessions = am_data.get("sessions", {})
    sections = am_data.get("sections", {})
    section_map = {sec_id: sdata.get("name") for sec_id, sdata in sections.items()}

    git_wts = git_ops.list_git_worktrees(git_ops.get_repo_root())
    quests = store.list_all(include_archive=True)

    branch_to_quest = {q.branch: q for q in quests if q.branch}

    if getattr(args, "json", False):
        out = []
        for wt in git_wts:
            wt_path = wt.get("worktree", "")
            branch = wt.get("branch", "")
            matched_quest = branch_to_quest.get(branch)
            am_wt_meta = None
            am_session_meta = None
            for wid, wdata in am_wts.items():
                if wdata.get("path") == wt_path or wdata.get("branch") == branch:
                    am_wt_meta = wdata
                    break
            if am_wt_meta:
                wid = [k for k, v in am_wts.items() if v == am_wt_meta][0]
                for sid, sdata in am_sessions.items():
                    if sdata.get("worktreeId") == wid:
                        am_session_meta = sdata
                        break
            out.append({
                "worktree": wt_path,
                "branch": branch,
                "section": section_map.get(am_wt_meta.get("sectionId"), "Ungrouped") if am_wt_meta else "None",
                "quest_id": matched_quest.id if matched_quest else None,
                "quest_status": matched_quest.status if matched_quest else None,
                "session": am_session_meta.get("name") if am_session_meta else None,
            })
        print(json.dumps(out, indent=2))
        return

    print("=" * 78)
    print("🌲 PHYSICAL GIT WORKTREES & AGENT MANAGER REALITY MAPPING")
    print("=" * 78)
    print(f"Total Physical Worktrees on Disk: {len(git_wts)}\n")

    for wt in git_wts:
        wt_path = wt.get("worktree", "")
        branch = wt.get("branch", "")
        raw_branch = wt.get("raw_branch", "")

        matched_quest = branch_to_quest.get(branch)
        if not matched_quest:
            for q in quests:
                if q.worktree and Path(q.worktree).resolve() == Path(wt_path).resolve():
                    matched_quest = q
                    break

        am_wt_meta = None
        am_session_meta = None
        for wid, wdata in am_wts.items():
            if wdata.get("path") == wt_path or wdata.get("branch") == branch:
                am_wt_meta = wdata
                break

        if am_wt_meta:
            wid = [k for k, v in am_wts.items() if v == am_wt_meta][0]
            for sid, sdata in am_sessions.items():
                if sdata.get("worktreeId") == wid:
                    am_session_meta = sdata
                    break

        section_name = section_map.get(am_wt_meta.get("sectionId"), "Ungrouped") if am_wt_meta else "External / None"
        quest_str = f"{matched_quest.id} [{matched_quest.status}] - {matched_quest.title}" if matched_quest else "No linked Quest"

        print(f"📁 {wt_path}")
        print(f"   • Branch:   {branch or raw_branch or 'detached'}")
        print(f"   • Section:  {section_name}")
        print(f"   • Quest:    {quest_str}")
        if am_session_meta:
            print(f"   • Session:  {am_session_meta.get('name', am_session_meta.get('id'))} [{am_session_meta.get('activity', 'idle')}]")
        print("-" * 78)


def cmd_ui(args):
    from court import ui_server
    if args.open:
        import threading, webbrowser
        threading.Timer(
            0.8, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    ui_server.serve(port=args.port)


# Lifecycle teardown sweep constants (Q707-era: 4 stranded scaffolding
# worktrees, 2 superseded easels, old cogship-245, and a serf alive since
# Sunday on a punished quest — all found by hand).
TEARDOWN_DONE_GRACE_HOURS = 24.0
_TEARDOWN_SWEEP_SESSION_STATES = ("PUNISHED", "DONE", "READY_TO_RAZE")


def monitor_lifecycle_sweep(now: Optional[datetime] = None) -> dict:
    """Classify sessions whose quests are past their useful life:
      - punished/done/ready-to-raze quests whose worker session id is stale
        (updated more than TEARDOWN_DONE_GRACE_HOURS ago, or whose process is
        simply gone) -> candidates for `kilo session delete`.
      - fork-scaffolding worktrees (master_of_coin / gatekeeper) on such
        quests -> candidates for `court fork-teardown-list` triage.
    Pure classification (no kills) so it is unit-testable and cron-safe."""
    now = now or datetime.now(timezone.utc)
    stale_sessions: list[dict] = []
    scaffolding: list[dict] = []
    for q in store.list_all():
        if q.status not in _TEARDOWN_SWEEP_SESSION_STATES:
            continue
        finished_at = q.updated_at or ""
        age_h: Optional[float] = None
        try:
            finished = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            age_h = (now - finished).total_seconds() / 3600.0
        except Exception:
            age_h = None
        for field_name in ("serf_session_id", "master_of_coin_session_id", "gatekeeper_session_id", "artist_session_id"):
            sid = (getattr(q, field_name, "") or "").strip()
            if not sid:
                continue
            # PID-fallback ids (kilo-serf-12345) have no real session behind
            # them; real ids (ses_*) are deletable only past the grace window.
            if sid.startswith("ses_") and age_h is not None and age_h < TEARDOWN_DONE_GRACE_HOURS:
                continue
            stale_sessions.append({
                "quest": q.id, "status": q.status, "field": field_name,
                "session_id": sid, "age_hours": round(age_h, 1) if age_h is not None else None,
                "worktree": q.worktree or "",
            })
        if q.status in ("PUNISHED", "DONE") and q.worktree and (
            "coin" in q.worktree or "gatehouse" in q.worktree or "master-of-coin" in q.worktree
        ):
            scaffolding.append({"quest": q.id, "status": q.status, "worktree": q.worktree})
    return {"stale_sessions": stale_sessions, "scaffolding_worktrees": scaffolding}


def cmd_monitor(args):
    """One-shot status roll-up + lifecycle sweep for the recurring cron.

    Runs the classified `court sync --all` (branch-ahead folds branch-wins;
    castle PUNISHED/HELD is royal and pushes down; castle-ahead charters sync
    down into idle worktrees), then reports lifecycle teardown candidates.
    Exits 0 always — a cron job must not page on routine drift."""
    print("== court monitor — status roll-up ==")
    try:
        rc = subprocess.run(
            [sys.executable, "-m", "court.cli", "sync", "--all"],
            cwd=str(git_ops.get_repo_root()),
            capture_output=True, text=True, timeout=600,
        )
        out = (rc.stdout or "").strip()
        print(out if out else "(sync produced no output)")
        if rc.returncode != 0:
            print(f"⚠️  sync exited {rc.returncode}: {(rc.stderr or '').strip()[:300]}")
    except Exception as e:
        print(f"⚠️  sync failed: {e}")

    print()
    print("== lifecycle teardown sweep ==")
    sweep = monitor_lifecycle_sweep()
    stale = sweep["stale_sessions"]
    scaff = sweep["scaffolding_worktrees"]
    if not stale and not scaff:
        print("Nothing past its useful life — no teardown candidates.")
        return
    if stale:
        print(f"Stale sessions on finished/frozen quests ({len(stale)}):")
        for s in stale:
            age = f"{s['age_hours']}h" if s["age_hours"] is not None else "age unknown"
            print(f"  * {s['quest']} [{s['status']}] {s['field']}={s['session_id']} ({age})")
        print("  → `kilo session delete <id>` per line (or `court teardown-list` for worktree state)")
    if scaff:
        print(f"Scaffolding worktrees on finished/frozen quests ({len(scaff)}):")
        for s in scaff:
            print(f"  * {s['quest']} [{s['status']}] — {s['worktree']}")
        print("  → run `court fork-teardown-list` for the move/stop/triage order")


def cmd_teardown_list(args):
    import json
    am_path = agent_manager_json_path()
    am_data = {}
    if am_path.exists():
        try:
            am_data = json.loads(am_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    am_wts = am_data.get("worktrees", {})
    am_sessions = am_data.get("sessions", {})
    ashes_section_id = None
    for sec_id, sec_data in am_data.get("sections", {}).items():
        if sec_data.get("name") == "Ashes":
            ashes_section_id = sec_id
            break

    quests = [q for q in store.list_all() if q.status == "READY_TO_RAZE"]
    if not quests:
        print("(nothing queued for teardown)")
        return

    active_in_ashes = []
    active_other_lane = []
    already_pruned = []

    for q in quests:
        branch = q.branch or ""
        found_wt_id = None
        found_wt_info = None
        for wid, wdata in am_wts.items():
            if wdata.get("branch") == branch or wid == q.worktree:
                found_wt_id = wid
                found_wt_info = wdata
                break

        if not found_wt_id or not found_wt_info or not Path(found_wt_info.get("path", "")).exists():
            already_pruned.append(q)
            continue

        wt_path = found_wt_info.get("path", "")
        sec_id = found_wt_info.get("sectionId", "")
        
        # Check diff alignment vs castle
        diff_res = git_ops._run(["git", "-C", wt_path, "rev-list", "--left-right", "--count", f"castle...{branch}"], Path.cwd())
        behind, ahead = "0", "0"
        if diff_res.get("ok") and diff_res.get("stdout"):
            parts = diff_res["stdout"].split()
            if len(parts) == 2:
                behind, ahead = parts[0], parts[1]

        st_res = git_ops._run(["git", "-C", wt_path, "status", "--porcelain"], Path.cwd())
        dirty_count = len(st_res.get("stdout", "").splitlines()) if st_res.get("stdout") else 0

        info = {
            "quest": q,
            "wt_id": found_wt_id,
            "path": wt_path,
            "sec_id": sec_id,
            "behind": behind,
            "ahead": ahead,
            "dirty_count": dirty_count,
        }

        if sec_id == ashes_section_id:
            active_in_ashes.append(info)
        else:
            active_other_lane.append(info)

    print("=" * 76)
    print(f"🪦 THE COURT TEARDOWN LIST — WORKTREES AWAITING DELETION IN ASHES")
    print("=" * 76)

    if active_in_ashes:
        print(f"\n🔥 Resting in Ashes Section (Safe for M'Lord to delete by hand) ({len(active_in_ashes)}):")
        for item in active_in_ashes:
            q = item["quest"]
            aligned = "✅ Aligned (0 drift)" if item["behind"] == "0" and item["ahead"] == "0" and item["dirty_count"] == 0 else f"⚠️ Drift (behind={item['behind']}, ahead={item['ahead']}, dirty={item['dirty_count']})"
            print(f"   * {q.id}: {item['wt_id']} ({item['path']}) [{aligned}]")

    if active_other_lane:
        print(f"\n⚠️ In READY_TO_RAZE but not yet moved to Ashes ({len(active_other_lane)}):")
        for item in active_other_lane:
            q = item["quest"]
            print(f"   * {q.id}: {item['wt_id']} ({item['path']}) [Section: {item['sec_id']}]")

    if already_pruned:
        print(f"\n📦 Already Pruned from Disk ({len(already_pruned)} Quests ready to archive):")
        for q in already_pruned:
            print(f"   * {q.id} (branch={q.branch or '-'})")
        print(f"\n   👉 Archive all {len(already_pruned)} pruned quests with: python3 -m court.cli raze")

    print("\n" + "=" * 76)


_FORK_QNUM_RE = re.compile(r"[Qq](\d+)")


def cmd_fork_teardown_list(args):
    """List ephemeral Master of Coin fork worktrees and Gatekeeper convoy
    worktrees whose job is done, and say exactly what the Steward should do
    next: MOVE (while the session is still alive) -> STOP -> never `git
    worktree remove` (that desyncs Agent Manager's own bookkeeping into a
    stale, unkillable session entry -- physical deletion is always M'Lord's
    manual action in the Agent Manager UI, same as `teardown-list`).

    These worktrees are NOT Quests themselves -- they are disposable review
    scaffolding Agent Manager forked because it can only ever create a new
    worktree, never attach a fresh session to an existing one (see AGENTS.md
    "Master of Coin / Gatekeeper Worktree Forking..."). This command cross-
    references `.kilo/agent-manager.json` against the real Quest ledger to
    tell ELIGIBLE (verdict/promotion already confirmed synced onto the real
    branch -- safe to move+stop) apart from HOLD (not synced yet -- needs a
    re-prompt, never a teardown).
    """
    import json

    am_path = agent_manager_json_path()
    am_data = {}
    if am_path.exists():
        try:
            am_data = json.loads(am_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    am_wts = am_data.get("worktrees", {})
    am_sessions = am_data.get("sessions", {})
    sections = am_data.get("sections", {})
    section_name_to_id = {sdata.get("name"): sec_id for sec_id, sdata in sections.items()}
    treasury_id = section_name_to_id.get("The Treasury") or section_name_to_id.get("Ashes")
    ashes_id = section_name_to_id.get("Ashes")

    quests = store.list_all(include_archive=True)
    by_id = {q.id.split("-")[0].upper(): q for q in quests}
    branch_to_quest = {q.branch: q for q in quests if q.branch}
    DONE_ENOUGH = {"GATE", "READY_TO_RAZE", "DONE", "PUNISHED"}

    def _session_for(wt_id):
        for sid, sdata in am_sessions.items():
            if sdata.get("worktreeId") == wt_id:
                return sid, sdata
        return None, None

    moc_eligible, moc_hold, gate_eligible, gate_hold = [], [], [], []

    for wt_id, wdata in am_wts.items():
        branch = wdata.get("branch", "") or ""
        if not branch or branch in ("main", "castle") or branch in branch_to_quest:
            continue  # real trunk or a real, still-canonical Quest/Serf worktree -- not a fork

        sec_id = wdata.get("sectionId", "")
        sid, sdata = _session_for(wt_id)

        if re.match(r"^the-gatehouse/", branch, re.IGNORECASE):
            station = branch.split("/", 1)[1]
            packed = [q for q in quests if q.cogship_id and (q.cogship_id == station or station.endswith(q.cogship_id))]
            entry = {"wt_id": wt_id, "branch": branch, "sec_id": sec_id, "session_id": sid,
                      "session_name": sdata.get("name") if sdata else None, "quests": packed}
            if not packed:
                entry["reason"] = "No Quest references this station/cogship_id -- unreferenced, safe to move."
                gate_eligible.append(entry)
            elif all(q.status in DONE_ENOUGH for q in packed):
                entry["reason"] = f"All {len(packed)} packed Quest(s) at/past READY_TO_RAZE."
                gate_eligible.append(entry)
            else:
                unfinished = [q for q in packed if q.status not in DONE_ENOUGH]
                entry["reason"] = f"{len(unfinished)} packed Quest(s) not yet promoted: " + ", ".join(f"{q.id}[{q.status}]" for q in unfinished)
                gate_hold.append(entry)
            continue

        m = _FORK_QNUM_RE.findall(branch)
        if not m:
            continue  # doesn't reference any Quest ID -- out of scope for this command
        quest = by_id.get(f"Q{m[-1]}")  # last Q-number wins (epic-child forms embed epic id first)
        if not quest or not quest.branch or quest.branch == branch:
            continue  # no matching Quest, or this literally IS that Quest's canonical branch

        verdict = (quest.body_sections.get("Master of Coin's Audit", "") or "").strip()
        entry = {"wt_id": wt_id, "branch": branch, "sec_id": sec_id, "session_id": sid,
                  "session_name": sdata.get("name") if sdata else None, "quest": quest}
        if quest.status in DONE_ENOUGH and "verdict" in verdict.lower():
            entry["reason"] = f"{quest.id} at {quest.status}; verdict confirmed synced onto real branch."
            moc_eligible.append(entry)
        else:
            entry["reason"] = f"{quest.id} still {quest.status}; no synced verdict on real branch yet."
            moc_hold.append(entry)

    if getattr(args, "json", False):
        def _ser(e):
            d = {k: v for k, v in e.items() if k not in ("quest", "quests")}
            if "quest" in e:
                d["quest_id"] = e["quest"].id
            if "quests" in e:
                d["quest_ids"] = [q.id for q in e["quests"]]
            return d
        print(json.dumps({
            "moc_eligible": [_ser(e) for e in moc_eligible],
            "moc_hold": [_ser(e) for e in moc_hold],
            "gatekeeper_eligible": [_ser(e) for e in gate_eligible],
            "gatekeeper_hold": [_ser(e) for e in gate_hold],
        }, indent=2))
        return

    print("=" * 78)
    print("🗄️  FORK TEARDOWN LIST — MASTER OF COIN & GATEKEEPER SCAFFOLDING")
    print("=" * 78)
    print("Order is MOVE (while session is alive) -> STOP -> never `git worktree")
    print("remove` (M'Lord prunes the directory by hand).\n")

    if moc_eligible:
        print(f"✅ ELIGIBLE — Master of Coin forks, verdict synced, safe to move+stop ({len(moc_eligible)}):")
        for e in moc_eligible:
            q = e["quest"]
            target = treasury_id or "<create a 'The Treasury' or 'Ashes' section first>"
            print(f"   * {q.id} [{q.status}] — {e['branch']}")
            print(f"     {e['reason']}")
            if e["session_id"]:
                print(f"     👉 kilo session delete sessionID={e['session_id']}   (session no longer needed; M'Lord prunes the directory by hand)")
            else:
                print(f"     ⚠️  Session already gone — worktree stranded in section {e['sec_id'] or '(ungrouped)'}."
                      f" Cannot be moved by this tool anymore (move requires a live session); ask M'Lord to drag"
                      f" it into Ashes/The Treasury by hand, or just leave it — it is inert.")
        print()

    if moc_hold:
        print(f"⏸  HOLD — Master of Coin forks whose verdict has NOT synced back yet ({len(moc_hold)}):")
        for e in moc_hold:
            q = e["quest"]
            print(f"   * {q.id} [{q.status}] — {e['branch']}")
            print(f"     {e['reason']}")
            if e["session_id"] and e["session_name"]:
                print(f"     👉 kilo run --agent master_of_coin --dir <this fork worktree> --model \"$(python3 -m court.cli model master_of_coin)\": \"Confirm your verdict is written under "
                      f"## Master of Coin's Audit in .court/quests/{q.id}*.md, then run `git push . HEAD:{q.branch}` "
                      f"from this fork worktree and report back the exact push result. Do not stop "
                      f"your own session.\"")
            else:
                print(f"     ⚠️  No live session left to re-prompt — this audit stalled without ever finishing."
                      f" Re-dispatch a fresh Master of Coin session per master_of_coin_review_prompt.md rather than"
                      f" resurrecting this one; this fork stays put (do not touch) until superseded.")
        print()

    if gate_eligible:
        print(f"✅ ELIGIBLE — Gatekeeper convoy worktrees, promotion confirmed, safe to move+stop ({len(gate_eligible)}):")
        for e in gate_eligible:
            print(f"   * {e['branch']}")
            print(f"     {e['reason']}")
            if e["session_id"]:
                print(f"     👉 kilo session delete sessionID={e['session_id']}   (promotion confirmed; M'Lord prunes the directory by hand)")
            else:
                print(f"     ⚠️  Session already gone — worktree stranded in section {e['sec_id'] or '(ungrouped)'}."
                      f" Ask M'Lord to drag it into Ashes by hand, or leave it — it is inert.")
        print()

    if gate_hold:
        print(f"⏸  HOLD — Gatekeeper convoys with unpromoted Quests still packed ({len(gate_hold)}):")
        for e in gate_hold:
            print(f"   * {e['branch']}")
            print(f"     {e['reason']}")
        print()

    if not (moc_eligible or moc_hold or gate_eligible or gate_hold):
        print("(nothing to report — no Master of Coin or Gatekeeper fork worktrees found)")

    print("=" * 78)


def cmd_archive(args):
    auto_commit = not getattr(args, "no_commit", False)
    dst = store.archive(args.quest_id, auto_commit=auto_commit, commit_msg=f"court: archive {args.quest_id}")
    print(f"Archived -> {dst}")


def cmd_rollup(args):
    quests = resolve_quest_selection(args, batchable=True, required=False)
    results = store.rollup_section(
        section_name=args.section,
        quests=quests,
    )
    if not results:
        print(f"(no rendered {args.section} found matching filters)")
        return

    print("=" * 72)
    print(f"COURT ROLLUP — {args.section.upper()} ({len(results)} Quests)")
    print("=" * 72)
    for quest, content in results:
        print(f"\n### {quest.id} ({quest.title}) [{quest.status}]")
        print(content)


def cmd_tally(args):
    quests = resolve_quest_selection(args, batchable=True, required=False)
    results = store.rollup_section(
        section_name="tally",
        quests=quests,
    )
    if not results:
        print("(no rendered production verification runbooks/tallies found matching filters)")
        return

    print("=" * 76)
    print(f"🔍 THE COURT TALLY — PRODUCTION & UI VERIFICATION RUNBOOKS ({len(results)} Quests)")
    print("=" * 76)
    for quest, content in results:
        print(f"\n### {quest.id}: {quest.title} ({quest.app}) [{quest.status}]")
        print(content)


def cmd_edict(args):
    current = store.load_edicts()
    if args.content or args.file:
        new_text = Path(args.file).read_text(encoding="utf-8") if args.file else args.content
        if args.append and current:
            updated = current + "\n\n" + f"- **{now_iso()[:10]}**: {new_text.strip()}"
        else:
            updated = f"# Royal Edicts & Decrees\n\n- **{now_iso()[:10]}**: {new_text.strip()}"
        auto_commit = not getattr(args, "no_commit", False)
        store.save_edicts(updated, auto_commit=auto_commit, commit_msg="court: update royal edicts")
        print("Updated .court/EDICTS.md")
    else:
        if current:
            print(current)
        else:
            print("(no royal edicts recorded; add with: python3 -m court.cli edict 'Your priority')")


def cmd_ship(args):
    # v1332 double-ship guard: two operators (or two agents) packing the
    # manifest concurrently produce two Cog Ship manifests for one convoy.
    require_op_lock("ship")
    base_branch = getattr(args, "base", "main")
    head_branch = getattr(args, "head", "castle")

    target_quests = resolve_quest_selection(
        args, batchable=True, required=False, default_status="READY_TO_RAZE,DONE"
    )
    explicit_selection = bool(getattr(args, "quest_ids", None) or getattr(args, "quest_id", None))
    if not explicit_selection:
        target_quests = [
            q for q in target_quests
            if q.kind != "scout" and q.section != "Investigation"
            and not git_ops.is_quest_merged_into(q, target_ref=base_branch)
        ]
    manifest = store.rollup_ship_manifest(quests=target_quests)
    quests = manifest["quests"]

    ab = git_ops.get_ahead_behind(base_branch, head_branch)
    log_res = git_ops.get_branch_log(base_branch, head_branch, max_count=30)
    diff_res = git_ops.get_branch_diffstat(base_branch, head_branch)

    print("=" * 76)
    print(f"🚢 COG SHIP DEPLOYMENT CONVOY — TRIBUTE ENTERING THE CASTLE ({len(quests)} Quests)")
    print("=" * 76)

    if ab.get("ok"):
        ahead = ab.get("ahead", 0)
        behind = ab.get("behind", 0)
        print(f"\n🏰 Branch Promotion Vector: {head_branch} -> {base_branch}")
        print(f"   * Status: {head_branch} is {ahead} commits ahead, {behind} commits behind {base_branch}")
        if log_res.get("ok") and log_res.get("stdout"):
            print(f"\n📦 Shipped Commits on {head_branch} ahead of {base_branch}:")
            for line in log_res["stdout"].splitlines()[:20]:
                print(f"   - {line}")
            if len(log_res["stdout"].splitlines()) > 20:
                print(f"   ... ({len(log_res['stdout'].splitlines()) - 20} more commits)")
        if diff_res.get("ok") and diff_res.get("stdout"):
            print(f"\n📊 Aggregate Diffstat ({base_branch}..{head_branch}):")
            for line in diff_res["stdout"].splitlines()[-5:]:
                print(f"   {line}")
    else:
        print(f"\n🏰 Branch Promotion Vector: {head_branch} (ready for deployment)")

    if quests:
        print(f"\n📋 Quests in Deployment Convoy ({len(quests)}):")
        for q in quests:
            parent_info = f" [Epic: {q.parent_epic}]" if q.parent_epic else ""
            cogship_info = f" [Cogship: {q.cogship_id}]" if q.cogship_id else ""
            forced_info = " ⚠️FORCED" if _last_ledger_entry_is_forced(q) else ""
            print(f"   * {q.id} ({q.app}): {q.title} [{q.status}]{parent_info}{cogship_info}{forced_info}")
    else:
        print("\n(no quests currently in READY_TO_RAZE or DONE matching filters)")

    # cogship-077 gate: surface any manifest quest whose FORCED promotion
    # marker failed the post-promotion code-presence audit — this manifest
    # must not be treated as deployable paperwork until re-verified.
    unverified_forced = []
    for q in quests:
        if _last_ledger_entry_is_forced(q):
            promo = _code_presence_check(q)
            if not promo.get("ok"):
                unverified_forced.append((q.id, promo.get("reason")))
    if unverified_forced:
        print(f"\n🚫 UNVERIFIED FORCED PROMOTIONS ({len(unverified_forced)}) — deployable paperwork only, NOT verified code:")
        for qid, reason in unverified_forced:
            print(f"   * {qid}: {reason}")
        print("   Run `court verify-manifest --cogship <id>` after re-promotion; raze/commute refuse these until verified.")

    # 1. Bard Chronicle
    if manifest["ballads"]:
        print("\n" + "-" * 76)
        print(f"📜 THE BARD'S CHRONICLE — Narrative & Transformations ({len(manifest['ballads'])} Ballads)")
        print("-" * 76)
        for q, b in manifest["ballads"]:
            print(f"\n### {q.id}: {q.title} ({q.app})")
            print(b)

    # 2. Coffers Ledger
    if manifest["tributes"]:
        print("\n" + "-" * 76)
        print(f"💰 THE COFFERS LEDGER — Provable Deliverables & Commits ({len(manifest['tributes'])} Tributes)")
        print("-" * 76)
        for q, t in manifest["tributes"]:
            print(f"\n### {q.id}: {q.title} ({q.app})")
            print(t)

    # 3. Tally Runbook (Production & UI Verification)
    if manifest["tallies"]:
        print("\n" + "-" * 76)
        print(f"🔍 THE TALLY RUNBOOK — Production Verification & UI Paths ({len(manifest['tallies'])} Tallies)")
        print("-" * 76)
        for q, v in manifest["tallies"]:
            print(f"\n### {q.id}: {q.title} ({q.app})")
            print(v)

    # 4. Penance & Tech Debt
    if manifest["penances"]:
        print("\n" + "-" * 76)
        print(f"⚖️ THE SERF PENANCE — Technical Debt & Remediations ({len(manifest['penances'])} Penances)")
        print("-" * 76)
        for q, p in manifest["penances"]:
            print(f"\n### {q.id}: {q.title} ({q.app})")
            print(p)

    # 5. Humble Opinions
    if manifest["opinions"]:
        print("\n" + "-" * 76)
        print(f"💡 THE HUMBLE OPINIONS — Field Intelligence & Next Quests ({len(manifest['opinions'])} Opinions)")
        print("-" * 76)
        for q, o in manifest["opinions"]:
            print(f"\n### {q.id}: {q.title} ({q.app})")
            print(o)

    # 6. Commutation Manifest (Post-Deploy Kingdom Actions)
    if manifest.get("commutations"):
        print("\n" + "-" * 76)
        print(f"⚡ THE COMMUTATION MANIFEST — Post-Deployment Kingdom Actions ({len(manifest['commutations'])} Commutations)")
        print("-" * 76)
        print("ℹ️ Note: Standard database migrations are executed automatically during deployment via release commands.")
        print("   The Steward is responsible for executing the operational steps below immediately upon successful deployment.")
        for q, c in manifest["commutations"]:
            print(f"\n### {q.id}: {q.title} ({q.app})")
            print(c)

    # 6b. Commutations already executed and logged in the Cogship Log.
    if manifest.get("commutations_done"):
        done_ids = ", ".join(q.id for q, _ in manifest["commutations_done"])
        print(f"\n✅ Commutations Done ({len(manifest['commutations_done'])}) — already executed and logged in Cogship Log: {done_ids}")
        print("   (steer them with `court` /status — they are no longer outstanding post-deploy actions)")

    # 7. Extra Tribute Not Requested (Scope Smuggling / Over-Delivery Intelligence)
    if manifest.get("extra_tributes"):
        print("\n" + "-" * 76)
        print(f"🎁 EXTRA TRIBUTE NOT REQUESTED — Scope Smuggling & Over-Delivery ({len(manifest['extra_tributes'])} Quests)")
        print("-" * 76)
        for q, et in manifest["extra_tributes"]:
            print(f"\n### {q.id}: {q.title} ({q.app})")
            print(et)

    print("\n" + "=" * 76)
    print("🏰 COG SHIP DEPLOYMENT SUMMARY COMPLETE")
    print("=" * 76)

    # Q147: `--confirm` promotes the staged convoy into production. Everything
    # above stays a read-only report; only this branch mutates git/remote/Fly.
    if getattr(args, "confirm", False):
        rc = _execute_ship_deployment(args, base_branch, head_branch)
        sys.exit(rc)


FLY_CONFIGS = {
    # flyctl app alias -> config file at the repo root of the production checkout
    "web": "fly.toml",
    "worker": "fly-worker.toml",
    "worker-heavy": "fly-worker-heavy.toml",
}
FLY_DEPLOY_TIMEOUT_SECONDS = 3600  # builds + release_command can take ~20m+ per app


def _ship_find_main_checkout(base_branch: str) -> Optional[Path]:
    """Return the filesystem path of the git worktree that has `base_branch`
    checked out, or None. A merge needs a real working tree, so the production
    branch must be checked out somewhere (it normally is: the main checkout)."""
    for wt in git_ops.list_git_worktrees():
        if wt.get("branch") == base_branch and not wt.get("bare"):
            return Path(wt["worktree"])
    return None


def _ship_fly_configs(selection: list[str], repo_root: Path) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Resolve the requested `--fly-app` selection to (alias, config_path,
    fly_app_name) tuples. Returns (configs, errors). App names are parsed from
    the config files so output can name the real Fly app being deployed."""
    errors: list[str] = []
    resolved: list[tuple[str, str, str]] = []
    aliases = list(selection) if selection else list(FLY_CONFIGS)
    for alias in aliases:
        cfg_name = FLY_CONFIGS.get(alias)
        if not cfg_name:
            errors.append(f"unknown --fly-app {alias!r} (valid: {', '.join(FLY_CONFIGS)})")
            continue
        cfg_path = repo_root / cfg_name
        if not cfg_path.exists():
            errors.append(f"config not found: {cfg_path}")
            continue
        app_name = cfg_name  # fallback if parsing fails
        try:
            for line in cfg_path.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("app ") or line.strip().startswith("app="):
                    app_name = line.split("=", 1)[1].strip().strip("'\"")
                    break
        except Exception:
            pass
        resolved.append((alias, cfg_name, app_name))
    return resolved, errors


def _ship_preflight(main_wt: Path, base_branch: str, head_branch: str) -> tuple[bool, list[str]]:
    """Safety checks that must all pass before any mutation: production
    checkout exists on the right branch, clean working tree, head branch
    exists, and local branch not behind its origin twin (a non-fast-forward
    push would be rejected mid-deployment otherwise)."""
    problems: list[str] = []

    branch_res = git_ops._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], main_wt)
    current = branch_res.get("stdout", "").strip()
    if current != base_branch:
        problems.append(f"{main_wt} has '{current or 'detached HEAD'}' checked out, expected '{base_branch}'")

    status_res = git_ops._run(["git", "status", "--porcelain"], main_wt)
    if status_res.get("ok") is False:
        problems.append(f"git status failed in {main_wt}: {status_res.get('stderr')}")
    elif status_res.get("stdout"):
        files = status_res["stdout"].splitlines()
        problems.append(f"{base_branch} checkout is dirty ({len(files)} uncommitted files); clean it first")

    head_res = git_ops._run(["git", "rev-parse", "--verify", head_branch], main_wt)
    if not head_res.get("ok"):
        problems.append(f"source branch '{head_branch}' not found")

    # Reason: `git fetch origin <branch>` with no destination only updates
    # FETCH_HEAD, not refs/remotes/origin/<branch> — it silently no-ops the
    # tracking ref unless remote.origin.fetch happens to already have a
    # matching refspec configured. This repo's remote.origin.fetch is scoped
    # to castle only, so origin/main (and any other branch) can go stale for
    # months while every command that trusts it reports a false "behind"
    # signal. Force an explicit refspec so the tracking ref is always
    # refreshed to the real remote state, independent of machine-local
    # remote.origin.fetch config.
    fetch_refspec = f"+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}"
    fetch_res = git_ops._run(["git", "fetch", "origin", fetch_refspec], main_wt, timeout=120)
    if not fetch_res.get("ok"):
        problems.append(f"git fetch origin {fetch_refspec} failed: {fetch_res.get('stderr')}")
    else:
        behind_res = git_ops._run(["git", "rev-list", "--count", f"{base_branch}..origin/{base_branch}"], main_wt)
        if behind_res.get("ok") and behind_res.get("stdout", "").isdigit() and int(behind_res["stdout"]) > 0:
            problems.append(
                f"origin/{base_branch} is {behind_res['stdout']} commit(s) ahead of local {base_branch}; reconcile before shipping"
            )

    # Q432: migration-graph integrity preflight. Django checkouts only — a
    # non-Django castle has no migration graph to verify, so the stage is not
    # applicable there. Fail closed: at a deploy gate, "could not verify" must
    # never read as pass.
    if (main_wt / "manage.py").is_file():
        graph = migration_graph.check_migration_graph(main_wt)
        if not graph.get("ok"):
            problems.append(
                "migration graph preflight failed: "
                + (graph.get("reason") or "unknown graph failure")
            )

    return (len(problems) == 0), problems


def _ship_merge(main_wt: Path, base_branch: str, head_branch: str) -> dict:
    """Merge head_branch (castle) into base_branch (main) in the production
    checkout. On any merge failure the merge is aborted so the production
    checkout is never left mid-merge."""
    merge_res = git_ops._run(["git", "merge", head_branch, "--no-edit"], main_wt, timeout=300)
    if merge_res.get("ok"):
        already = "Already up to date" in merge_res.get("stdout", "") or "Already up to date" in merge_res.get(
            "stderr", ""
        )
        return {"ok": True, "already_up_to_date": already, "output": merge_res}
    git_ops._run(["git", "merge", "--abort"], main_wt)
    return {"ok": False, "already_up_to_date": False, "output": merge_res}


def _execute_ship_deployment(args, base_branch: str, head_branch: str) -> int:
    """The mutating half of `court ship --confirm`: merge `head_branch` (castle)
    into `base_branch` (main) and push `base_branch` to origin, kicking off the
    remote autodeploy pipeline. Returns process exit code (0 = success, 1 = error)."""
    print("\n" + "=" * 76)
    print(f"⚔️ DEPLOYMENT CONFIRMED — MERGING {head_branch} INTO {base_branch} & PUSHING TO ORIGIN")
    print("=" * 76)
    dry_run = getattr(args, "dry_run", False)
    if dry_run:
        print("🧪 DRY RUN: preflight checks will run; merge/push will NOT execute.\n")

    # Step 0 — locate the production checkout.
    main_wt = _ship_find_main_checkout(base_branch)
    if not main_wt:
        print(f"❌ No worktree has '{base_branch}' checked out. Check out {base_branch} in a worktree first, then retry.")
        return 1
    print(f"✅ Production checkout: {main_wt} (branch {base_branch})")

    # Step 0b — preflight safety checks.
    ok, problems = _ship_preflight(main_wt, base_branch, head_branch)
    if not ok:
        print("\n🚨 PREFLIGHT FAILED — nothing was modified:")
        for p in problems:
            print(f"   - {p}")
        return 1
    print("✅ Preflight: clean tree, correct branch, push is fast-forward")
    if dry_run:
        print("\n🧪 DRY RUN PASSED — all preflight checks green; no mutations performed.")
        return 0

    # Step 1 — merge head_branch into base_branch.
    print(f"\n🔀 Merging {head_branch} into {base_branch} ...")
    merge = _ship_merge(main_wt, base_branch, head_branch)
    if not merge["ok"]:
        out = merge["output"]
        print(f"❌ Merge failed (aborted, {base_branch} untouched):")
        print(f"   {out.get('stderr') or out.get('stdout')}")
        return 1
    if merge["already_up_to_date"]:
        print(f"✅ {base_branch} already contains {head_branch} (no merge needed)")
    else:
        print(f"✅ Merged {head_branch} -> {base_branch}")

    # Step 2 — push to origin.
    print(f"\n📤 Pushing {base_branch} to origin ...")
    push_res = git_ops._run(["git", "push", "origin", base_branch], main_wt, timeout=600)
    if not push_res.get("ok"):
        print(f"❌ Push failed: {push_res.get('stderr') or push_res.get('stdout')}")
        print(f"   The merge to local {base_branch} succeeded; reconcile the remote and re-push manually.")
        return 1
    print(f"✅ Pushed {base_branch} -> origin/{base_branch}")

    print("\n" + "=" * 76)
    print(f"🏰 SHIP CONFIRMED — {head_branch} merged into {base_branch} and pushed to origin (autodeploy triggered)")
    print("=" * 76)
    return 0


def cmd_audit(args):
    base_branch = getattr(args, "base", "castle") or "castle"
    target_quests = resolve_quest_selection(args, batchable=True, required=False)
    if not target_quests:
        print("(no quests matching filters)")
        return

    raw_ids = getattr(args, "quest_ids", None)
    if raw_ids and len(target_quests) == 1 and "," not in raw_ids:
        quest = target_quests[0]
        res = ward.audit_quest(quest, base_branch=base_branch)
        if getattr(args, "json", False):
            import json
            print(json.dumps(res.to_dict(), indent=2))
        else:
            print("=" * 76)
            print(res.format_report())
            print("=" * 76)
        return

    results = [ward.audit_quest(q, base_branch=base_branch) for q in target_quests]
    if getattr(args, "json", False):
        import json
        print(json.dumps([r.to_dict() for r in results], indent=2))
        return

    print("=" * 76)
    print(f"🛡️ THE WARD — WORKTREE & TRIBUTE AUDIT ({len(results)} Quests)")
    print("=" * 76)
    for r in results:
        print()
        print(r.format_report())
        print("-" * 76)


def cmd_levy(args):
    base_branch = getattr(args, "base", "castle") or "castle"
    target_quests = resolve_quest_selection(
        args, batchable=True, required=False, default_status="WORKING,DISPATCHED"
    )

    if not target_quests:
        print("(no active quests found matching levy criteria)")
        return

    # Mechanical Per-Quest Rebase-Then-Advance (Q149, 2026-09-05).
    # Root cause of "levy/collect can never reach 0 drift": castle is a
    # constantly-moving target (every Gatekeeper Cog Ship promotion advances
    # it), while the old flow expected an idle Serf *agent* to notice drift
    # and run `git merge castle` itself before TRIBUTE_READY. At real WIP that
    # notice-and-spawn-an-agent round-trip is slower than castle's advance
    # rate, so drift only ever grew (Q141/Q122/Q139 hit 164-194 commits
    # behind) and the WORKING/TRIBUTE_READY queue could never converge.
    #
    # A subtler version of the same race exists *within a single levy run*:
    # advancing quest A to TRIBUTE_READY commits onto castle (when levy itself runs
    # from castle), which moves castle's tip by one commit — so if the
    # mechanical rebase ran as one batch pre-flight step before the advance
    # loop, quest B (processed right after A in the same run) would see
    # `behind: 1` again by the time its own audit runs, and get blocked from
    # advancing even though it was perfectly synced moments earlier. So the
    # rebase for each quest must happen immediately before *that quest's own*
    # audit/advance decision, not once for the whole batch up front — every
    # quest gets rebased against castle's tip as of the moment it's actually
    # decided, including any advance castle just took from an earlier quest
    # in this same loop.
    rebase_notes: dict[str, str] = {}
    levied_list = []
    working_list = []
    violations_list = []
    pending_saves: dict[str, Quest] = {}

    for q in target_quests:
        # Resolve this Quest's worktree exactly once per run and pass it down
        # to every step below: find_worktree_for_quest costs 1-3 git subprocess
        # spawns per call, and the old flow re-resolved it in the rebase step,
        # sync, AND audit (up to ~9 wasted spawns per Quest per levy pass).
        wt = None
        if q.worktree and Path(q.worktree).is_dir():
            wt = Path(q.worktree)
        else:
            wt = git_ops.find_worktree_for_quest(q)

        # Step 0: Mechanical rebase onto base_branch, right before this
        # specific quest's own audit/advance decision (see note above).
        if getattr(args, "rebase", True) and q.status in ("WORKING", "TRIBUTE_READY", "DISPATCHED"):
            if wt and Path(wt).is_dir():
                rb = git_ops.rebase_worktree_onto_base(wt, base_branch=base_branch, own_quest_id=q.id)
                if rb.get("merged"):
                    auto_resolved = rb.get("auto_resolved_files") or []
                    foreign_note = f" (auto-resolved {len(auto_resolved)} foreign ledger conflict(s) to {base_branch}'s side)" if auto_resolved else ""
                    rebase_notes[q.id] = (
                        f"🔄 Mechanically rebased onto {base_branch}: "
                        f"{rb.get('before_behind')} -> {rb.get('after_behind')} behind.{foreign_note}"
                    )
                elif rb.get("conflict"):
                    sample = ", ".join(rb.get("conflicting_files", [])[:5]) or "unknown files"
                    rebase_notes[q.id] = (
                        f"⚠️  Mechanical rebase onto {base_branch} hit conflicts in: {sample} "
                        f"— aborted cleanly, needs Serf resolution (`git merge {base_branch}` by hand)."
                    )
                elif rb.get("skipped") == "dirty":
                    rebase_notes[q.id] = "⚠️  Skipped mechanical rebase: worktree has uncommitted changes."

        # Step 1: Auto-sync tribute and frontmatter from worktree if present
        # (write deferred to the end-of-run batched commit — castle's tip must
        # not move mid-sweep or every subsequent quest's rebase goes stale).
        if getattr(args, "sync", True):
            synced, _notes = ward.sync_tribute_from_worktree(q, worktree_path=wt, auto_commit=False)
            if synced:
                pending_saves[q.id] = q

        # Step 2: Sentry Audit — freshly re-reads git status now, so it sees
        # the rebase this quest just got (and any castle advance from an
        # earlier quest in this same loop), not a stale pre-flight snapshot.
        audit = ward.audit_quest(q, worktree_path=wt, base_branch=base_branch)

        # Step 3: Advance if requested and compliant
        did_advance = False
        if args.advance and audit.is_compliant and audit.tribute_present and q.status in ("WORKING", "DISPATCHED"):
            # Deferred Rebase Doctrine (Q146): drift behind castle is tolerated
            # during WORKING, but TRIBUTE_READY entry requires exactly one clean rebase
            # (behind == 0). The audit above ran under WORKING rules where
            # behind > 0 is only a warning — without this gate, --advance would
            # promote a drifted quest into TRIBUTE_READY and instantly mint a violation.
            behind = audit.git_status.get("behind")
            if behind is not None and behind > 0:
                audit.warnings.append(
                    f"Blocked auto-advance to TRIBUTE_READY: {behind} commit(s) behind {base_branch} "
                    f"(Serf must run the deferred rebase `git merge castle` first)."
                )
            else:
                q.set_status("TRIBUTE_READY", "Levied: Tribute synced from worktree and verified compliant")
                pending_saves[q.id] = q
                did_advance = True

        if audit.violations:
            violations_list.append((q, audit))
        elif q.status == "TRIBUTE_READY" or did_advance:
            levied_list.append((q, audit, did_advance))
        else:
            working_list.append((q, audit))

    # One batched commit for the whole levy sweep (was: one commit per synced/
    # advanced quest). Deferred to here so castle's tip stays frozen during the
    # sweep — every quest's mechanical rebase now sees the same castle tip,
    # which removes the intra-run behind:1 race the Q149 note worked around.
    if pending_saves:
        store.save_many(
            list(pending_saves.values()),
            f"court: levy sweep (synced/advanced {len(pending_saves)} quest(s))",
            auto_commit=not getattr(args, "no_commit", False),
        )

    if rebase_notes and not getattr(args, "json", False):
        print("🔄 MECHANICAL REBASE SWEEP (per-quest, interleaved with advance)")
        for qid, note in rebase_notes.items():
            print(f"  {qid}: {note}")
        print()

    if getattr(args, "json", False):
        import json
        out_data = {
            "rebase_sweep": rebase_notes,
            "levied": [a.to_dict() for _, a, _ in levied_list],
            "working": [a.to_dict() for _, a in working_list],
            "non_compliant": [a.to_dict() for _, a in violations_list],
        }
        print(json.dumps(out_data, indent=2))
        return

    print("=" * 78)
    print("⚖️ THE COURT LEVY — DETERMINISTIC WORKTREE TRIAGE & TRIBUTE SYNCHRONIZATION")
    print("=" * 78)
    print(f"Summary: {len(levied_list)} Levied (TRIBUTE_READY) | {len(working_list)} Working | {len(violations_list)} Action Needed / Blocked\n")

    if levied_list:
        print(f"🪙 LEVIED & READY FOR COIN AUDIT ({len(levied_list)})")
        for q, audit, advanced in levied_list:
            adv_str = " -> [TRIBUTE_READY] (Auto-advanced)" if advanced else " [TRIBUTE_READY]"
            sec_str = f"({len(audit.sections_present)} sections verified)"
            ahead_str = f"↑{audit.git_status.get('ahead', 0)}" if audit.git_status.get("ahead") is not None else ""
            task_str = f" [Tasks: {audit.task_progress['summary']}]" if audit.task_progress.get("found") else ""
            phase_str = f" | Phase: {audit.task_progress['active_phase']}" if audit.task_progress.get("active_phase") else ""
            print(f"  ✓ {q.id:36} {adv_str:28} {ahead_str:5} {sec_str}{task_str}{phase_str}")
            print(f"      {q.title}")

    if working_list:
        print(f"\n🔨 ACTIVE WORKING WORKTREES ({len(working_list)})")
        for q, audit in working_list:
            req_total = 5 if (q.kind == "scout" or q.section == "Investigation") else 6
            tribute_str = f"tribute: {len(audit.sections_present)}/{req_total} sections" if audit.tribute_present else "in-progress"
            ahead_str = f"↑{audit.git_status.get('ahead', 0)}" if audit.git_status.get("ahead") is not None else ""
            task_str = f"[Tasks: {audit.task_progress['summary']}] " if audit.task_progress.get("found") else ""
            phase_str = f" | Phase: {audit.task_progress['active_phase']}" if audit.task_progress.get("active_phase") else ""
            print(f"  • {q.id:36} [{q.status:10}] {task_str}{ahead_str:5} ({tribute_str}){phase_str}")
            print(f"      {q.title}")

    if violations_list:
        print(f"\n🔴 NON-COMPLIANT / DRIFTED / ACTION NEEDED ({len(violations_list)})")
        for q, audit in violations_list:
            behind_str = f"↓{audit.git_status.get('behind', 0)} behind" if audit.git_status.get("behind") else ""
            dirty_str = "DIRTY" if audit.git_status.get("dirty") else ""
            flags = " ".join(filter(None, [behind_str, dirty_str]))
            flags_str = f" [{flags}]" if flags else ""
            task_str = f" [Tasks: {audit.task_progress['summary']}]" if audit.task_progress.get("found") else ""
            print(f"  ✗ {q.id:36} [{q.status:10}]{flags_str}{task_str}")
            print(f"      {q.title}")
            for v in audit.violations:
                print(f"      🔴 {v}")
            for w in audit.warnings:
                print(f"      ⚠️  {w}")

    print("\n" + "=" * 78)
    print("💡 RECOMMENDED NEXT STEPS & ACTIONS:")
    print("  • For WORKING Quests showing [Tasks: X/Y (100%)] [CLEAN] zero drift (Serf-complete, paperwork-only): dispatch Master of Coin directly (`.court/templates/master_of_coin_review_prompt.md`) — do NOT `/goad`, that signature is paperwork, not code.")
    print("  • For WORKING Quests genuinely idle/incomplete (checklist < 100% and/or dirty tree): Run `/goad <quest_id>` to prod the session to update charter & continue.")
    print("  • For completed Quests in Review: Summon Master of Coin via `/levy <id>` (`.kilo/commands/levy.md`).")
    print("  • For Quests ready for Gatehouse integration: Run `/collect` (`.kilo/commands/collect.md`).")
    print("  • For a real merge conflict flagged above: dispatch the Serf to resolve it, then re-run `court rebase <id>`.")
    print("=" * 78)


def cmd_rebase(args):
    """Mechanically converge worktrees onto --base (default castle) via `git merge`,
    with zero LLM agent turns involved.

    Why this exists (Q149, 2026-09-05): `castle` moves continuously as the Gatekeeper
    promotes Cog Ships. The old flow expected an idle Serf *agent* to notice drift and
    run `git merge castle` itself before TRIBUTE_READY — at real WIP that notice-and-spawn
    round-trip is slower than castle's advance rate, so drift only ever grew
    (Q141/Q122/Q139 reached 164-194 commits behind) and the WORKING/TRIBUTE_READY queue could
    never reach zero drift. `court levy` now runs this sweep automatically as a
    pre-flight step; this standalone verb exists for direct/manual remediation (e.g.
    targeting GATE Quests, or a single stuck Quest) without waiting for a full levy pass.
    """
    base_branch = getattr(args, "base", "castle") or "castle"

    target_quests = resolve_quest_selection(
        args, batchable=True, required=False, default_status="WORKING,TRIBUTE_READY,DISPATCHED,GATE"
    )

    if not target_quests:
        print("(no quests found matching rebase criteria)")
        return

    results = []
    for q in target_quests:
        wt = None
        if q.worktree and Path(q.worktree).is_dir():
            wt = Path(q.worktree)
        else:
            wt = git_ops.find_worktree_for_quest(q)
        if not wt or not Path(wt).is_dir():
            results.append({"quest_id": q.id, "title": q.title, "skipped": "no-worktree"})
            continue

        if getattr(args, "dry_run", False):
            ab = git_ops.get_ahead_behind(base_branch, "HEAD", cwd=Path(wt))
            results.append({
                "quest_id": q.id, "title": q.title, "dry_run": True,
                "behind": ab.get("behind"), "ahead": ab.get("ahead"),
            })
            continue

        rb = git_ops.rebase_worktree_onto_base(wt, base_branch=base_branch, own_quest_id=q.id)
        rb["quest_id"] = q.id
        rb["title"] = q.title
        results.append(rb)

    if getattr(args, "json", False):
        print(json.dumps(results, indent=2))
        return

    print("=" * 78)
    print(f"🔄 THE COURT REBASE — MECHANICAL CONVERGENCE ONTO '{base_branch}' ({len(results)} Quests)")
    print("=" * 78)
    for r in results:
        qid = r.get("quest_id", "?")
        title = r.get("title", "")
        if r.get("skipped") == "no-worktree":
            print(f"  ⏭️  {qid:36} (no resolvable worktree — skipped)")
        elif r.get("dry_run"):
            print(f"  🔎 {qid:36} behind={r.get('behind')} ahead={r.get('ahead')}  {title}")
        elif r.get("skipped") == "dirty":
            print(f"  ⚠️  {qid:36} DIRTY — uncommitted changes, skipped. Commit/stash first.  {title}")
        elif r.get("already_up_to_date"):
            print(f"  ✅ {qid:36} already up to date (behind=0).  {title}")
        elif r.get("merged"):
            auto_resolved = r.get("auto_resolved_files") or []
            foreign_note = f"  [auto-resolved {len(auto_resolved)} foreign ledger conflict(s) -> {base_branch}]" if auto_resolved else ""
            print(f"  🔄 {qid:36} merged: {r.get('before_behind')} -> {r.get('after_behind')} behind.{foreign_note}  {title}")
        elif r.get("conflict"):
            sample = ", ".join(r.get("conflicting_files", [])[:5]) or "unknown files"
            print(f"  🔴 {qid:36} CONFLICT in [{sample}] — aborted, needs manual resolution.  {title}")
        else:
            print(f"  ❓ {qid:36} {r.get('error', 'unknown result')}  {title}")
    print("=" * 78)


def cmd_diff(args):
    try:
        quest = store.load(args.quest_id)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    wt = None
    if quest.worktree and Path(quest.worktree).is_dir():
        wt = Path(quest.worktree)
    else:
        wt = git_ops.find_worktree_for_quest(quest)

    if not wt or not wt.is_dir():
        print(f"ERROR: No active worktree found for {quest.id} (branch: {quest.branch or '-'})", file=sys.stderr)
        sys.exit(1)

    base = getattr(args, "base", "castle") or "castle"
    diff_res = git_ops.get_worktree_diff(
        wt,
        base=base,
        stat_only=args.stat,
        file_stats=True,
    )

    if not diff_res.get("ok"):
        print(f"ERROR: {diff_res.get('error')}", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "json", False):
        import json
        print(json.dumps(diff_res, indent=2))
        return

    print("=" * 76)
    print(f"📊 WORKTREE DIFF: {quest.id} ({wt.name}) vs {base}")
    print(f"Branch: {quest.branch} | Files Changed: {diff_res['total_files']} (+{diff_res['total_additions']}, -{diff_res['total_deletions']})")
    print("=" * 76)

    if args.numstat:
        for f in diff_res["files"]:
            bin_tag = " (binary)" if f["binary"] else ""
            print(f"{f['additions']:>6}\t{f['deletions']:>6}\t{f['path']}{bin_tag}")
    elif args.stat or not (args.patch or getattr(args, "full", False)):
        if diff_res.get("stat"):
            print(diff_res["stat"])
        else:
            print("(no diff vs base branch)")
    if args.patch or getattr(args, "full", False):
        if diff_res.get("patch"):
            print("\n" + diff_res["patch"])
        else:
            print("(no diff patch vs base branch)")


def cmd_fix_branches(args):
    if getattr(args, "current_worktree", False):
        res = branch_ops.realign_current_worktree(
            cwd=Path.cwd(),
            sync_castle=getattr(args, "sync", True),
        )
        if getattr(args, "json", False):
            print(json.dumps(res, indent=2))
        else:
            if res.get("renamed"):
                print(f"✅ Realigned branch: {res['previous_branch']} -> {res['canonical_branch']} ({res.get('reason')})")
            else:
                print(f"✅ Current branch is already canonical: {res['canonical_branch']}")
            if res.get("sync_castle") is True:
                print("✅ Fast-forward synced with castle (behind: 0).")
            elif res.get("sync_castle") is False:
                print(f"⚠️ Fast-forward sync with castle failed: {res.get('sync_error')}")
        if not res.get("ok"):
            sys.exit(1)
        return

    dry_run = getattr(args, "dry_run", False)
    res = branch_ops.realign_all_branches(dry_run=dry_run)

    if getattr(args, "json", False):
        print(json.dumps(res, indent=2))
        return

    prefix = "[DRY RUN] " if dry_run else ""
    print("=" * 76)
    print(f"🌿 {prefix}THE COURT BRANCH REALIGNMENT — FOLDER HIERARCHY AUDIT & REPAIR")
    print("=" * 76)
    print(f"Scanned {res['total_scanned']} local branches. Identified {res['realigned_count']} flat branches to realign.\n")

    if res.get("actions"):
        for a in res["actions"]:
            action = a["action_taken"]
            checked = f" (worktree: {a['worktree_path']})" if a["checked_out"] else " (loose branch)"
            print(f"  • {a['old_branch']} -> {a['new_branch']}{checked}")
            print(f"    Action: {action} | Source: {a['reason']}")

    if res.get("errors"):
        print("\n🚨 Errors encountered during realignment:")
        for err in res["errors"]:
            print(f"  🔴 {err['old_branch']} -> {err['new_branch']}: {err.get('error')}")

    if not res.get("actions") and not res.get("errors"):
        print("✅ All local branches and worktrees are already using canonical forward-slash folder hierarchy.")

    print("\n" + "=" * 76)
    if not res.get("ok"):
        sys.exit(1)


# ---------------------------------------------------------------------------
# Shared studio browser verbs (thin wrappers over court.browser)
# ---------------------------------------------------------------------------


def cmd_browser_start(args):
    annotate_wt = Path(args.annotate).expanduser().resolve() if getattr(args, "annotate", None) else None
    if annotate_wt and not annotate_wt.is_dir():
        print(f"ERROR: --annotate worktree does not exist: {annotate_wt}", file=sys.stderr)
        sys.exit(1)
    try:
        info = studio_browser.start_browser(
            headless=bool(getattr(args, "headless", False)),
            port=getattr(args, "port", None),
            profile=None,
            annotate_worktree=annotate_wt,
        )
    except Exception as e:
        print(f"ERROR: could not start the shared studio browser: {e}", file=sys.stderr)
        sys.exit(1)
    print("🌐 Shared studio browser is up:")
    print(f"  CDP:      {info.get('cdp_url')}")
    print(f"  Port:     {info.get('port')}")
    print(f"  PID:      {info.get('pid')}")
    print(f"  Profile:  {info.get('profile')}")
    print(f"  Headless: {info.get('headless')}")
    annotator_pid = info.get("annotator_pid")
    if annotate_wt:
        print(f"  Injector: {'PID ' + str(annotator_pid) if annotator_pid else 'not started'} (worktree: {annotate_wt})")
        print(f"  Notes:    {Path(annotate_wt) / '.kilo' / 'studio-annotations.jsonl'}")
    print(f"  State:    {studio_browser.STATE_PATH}")
    if info.get("headless"):
        print(f"  Attach:   npx chrome-devtools-mcp@latest --browserUrl {info.get('cdp_url')}")


def cmd_browser_status(args):
    info = studio_browser.browser_status()
    if getattr(args, "json", False):
        print(json.dumps(info, indent=2))
        return
    recorded = info.get("recorded")
    if not recorded:
        print("🌐 No shared studio browser recorded (never started, or state file removed).")
        return
    if info.get("running"):
        print("🌐 Shared studio browser is RUNNING:")
        print(f"  CDP:      http://127.0.0.1:{recorded.get('port')}")
        print(f"  PID:      {recorded.get('pid')} ({info.get('cdp', {}).get('browser') or 'chromium'})")
        print(f"  Profile:  {recorded.get('profile')}")
        print(f"  Injector: {'alive (PID ' + str(recorded.get('annotator_pid')) + ')' if info.get('annotator_alive') else 'not running'}")
    else:
        print("🌐 Shared studio browser is NOT running (stale state file):")
        print(f"  Recorded PID: {recorded.get('pid')} — alive: {info.get('pid_alive')} — CDP: {info.get('cdp_responsive')}")
        print("  Start it with: python3 -m court.cli browser start")


def cmd_browser_stop(args):
    result = studio_browser.stop_browser()
    if not result.get("had_state"):
        print("🌐 Nothing to stop (no state file).")
        return
    if not result.get("actions"):
        print("🌐 Stopped: state file cleared (recorded processes were already gone).")
        return
    for action in result["actions"]:
        outcome = action.get("outcome", "")
        for key, value in action.items():
            if key != "outcome":
                print(f"  {key} {value}: {outcome}")
    print("🌐 Shared studio browser stopped.")


def cmd_browser_inject_worker(args):
    studio_browser._inject_worker(args.worktree)


def build_parser():
    p = argparse.ArgumentParser(prog="court", description="Deterministic Court ledger CLI")
    sub = p.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="Create a new Quest or Epic")
    p_new.add_argument("--app", required=True)
    p_new.add_argument("--concern", required=True)
    p_new.add_argument("--title", required=True)
    p_new.add_argument("--section", choices=SECTIONS, default="")
    p_new.add_argument("--tags", default="", help="Tags matching section/category (e.g. Feature, Optimization)")
    p_new.add_argument("--branch", default="", help="Branch name override (defaults to tree format)")
    p_new.add_argument("--kind", choices=KINDS, default="quest")
    p_new.add_argument("--epic", default="", help="Parent Epic Quest id, if any")
    p_new.add_argument("--scout-of", default="", dest="scout_of", help="Source Scout Quest id this production Quest implements, if any (Q126 Scout-to-Quest lifecycle)")
    p_new.add_argument("--goal", default="", help="The Kingdom Requires body text (the Quest's goal & scope)")
    p_new.add_argument("--tribute", default="", help="Expected Tribute body text")
    p_new.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_new.set_defaults(func=cmd_new)

    p_patchwork = sub.add_parser(
        "patchwork",
        help="Fast-track hotfix pipeline: one-shot intake, serf dispatch, inline test verification, and promotion",
    )
    p_patchwork.add_argument("quest_id", nargs="?", default=None, help="Optional Quest ID to verify, collect, promote, or inspect")
    p_patchwork.add_argument("--app", default=None, help="Application name for new hotfix")
    p_patchwork.add_argument("--concern", default=None, help="Concern slug for new hotfix")
    p_patchwork.add_argument("--title", default=None, help="Short title describing hotfix")
    p_patchwork.add_argument("--goal", default=None, help="The Kingdom Requires / technical fix specification")
    p_patchwork.add_argument("--test-cmd", default=None, help="Verification test command to run in worktree")
    p_patchwork.add_argument("--branch", default=None, help="Branch name override (must start with quest/...)")
    p_patchwork.add_argument("--tags", default="", help="Tags override (defaults to 'Bug fix,hotfix')")
    p_patchwork.add_argument(
        "--model", "--serf-model",
        dest="serf_model",
        default=None,
        help="Worker model (default: manifest models.serf in .court/config.json)",
    )
    p_patchwork.add_argument("--base", default="castle", help="Base branch (default: castle)")
    p_patchwork.add_argument("--cogship", default=None, help="Cog Ship ID for collection (default: auto-allocated)")
    p_patchwork.add_argument("--verify", action="store_true", help="Run in-tree test verification for the hotfix")
    p_patchwork.add_argument("--collect", action="store_true", help="Inline gatehouse pack & audit for hotfix")
    p_patchwork.add_argument("--promote", action="store_true", help="Inline test verification, merge to castle, and Cog Ship stamp")
    p_patchwork.add_argument("--run", action="store_true", default=True, help="Run kilo headless command immediately (default: True)")
    p_patchwork.add_argument("--no-run", action="store_true", help="Configure worktree without launching background session")
    p_patchwork.add_argument("--native", action="store_true", help="Force native git worktree without Kilo CLI")
    p_patchwork.add_argument("--create-worktree", action="store_true", help="Create native git worktree automatically")
    p_patchwork.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_patchwork.add_argument("--force-stamp", action="store_true", help="Allow re-stamping a quest already stamped on another cogship (duplicate-convoy guard override)")
    p_patchwork.set_defaults(func=cmd_patchwork)

    p_charter = sub.add_parser(
        "charter",
        help="Composite: fold M'Lord's notes, idempotently advance to PLANNED, compute branch, print Serf-dispatch NEXT STEPS (Q183)",
    )
    p_charter.add_argument("quest_id")
    p_charter.add_argument("--notes", default="", help="M'Lord's charter notes, appended to 'The Kingdom Requires'")
    p_charter.add_argument(
        "--pillory-of", dest="pillory_of", default=None,
        help="Link this successor to a PUNISHED predecessor Quest (atomically sets "
             "pillory_of here and pilloried_by there, in one command — Q185's "
             "dedicated replacement for the old raw set-field pair)",
    )
    p_charter.add_argument(
        "--section",
        choices=("Bug fix", "Feature", "Optimization"),
        default=None,
        help="Override the dispatch lane label for the dispatch NEXT STEPS",
    )
    p_charter.add_argument("--dispatch", action="store_true", help="Immediately stand up worktree and dispatch to WORKING")
    p_charter.add_argument("--standup", action="store_true", help="Automate Kilo worktree creation and standup")
    p_charter.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_charter.set_defaults(func=cmd_charter)

    p_dispatch = sub.add_parser(
        "dispatch",
        help="Dispatch a Quest to a worktree (creates worktree and configures agent mode, advances to WORKING)",
    )
    p_dispatch.add_argument("quest_id", help="Quest ID to dispatch")
    p_dispatch.add_argument("--branch", default=None, help="Canonical Quest branch (defaults to chartered branch)")
    p_dispatch.add_argument("--worktree", default=None, help="Quest worktree path")
    p_dispatch.add_argument("--create-worktree", action="store_true", help="Create native git worktree automatically")
    p_dispatch.add_argument("--standup", action="store_true", help="Automate Kilo worktree creation, worktree agent config, and session standup")
    p_dispatch.add_argument("--native", action="store_true", help="Force native git worktree without Kilo CLI")
    p_dispatch.add_argument("--run", action="store_true", default=True, help="Run kilo headless command immediately (default: True)")
    p_dispatch.add_argument("--no-run", action="store_true", help="Configure worktree and write task without launching background Kilo session")
    p_dispatch.add_argument("--agent", default=None, help="Agent to use (default: serf, or scout for investigations)")
    p_dispatch.add_argument("--prompt", default=None, help="Custom prompt override (defaults to pure charter task instructions)")
    p_dispatch.add_argument("--base", default="castle", help="Base branch for new worktrees (default: castle)")
    p_dispatch.add_argument("--session-id", default=None, dest="session_id", help="Agent session ID (defaults to 'native')")
    p_dispatch.add_argument(
        "--model", "--serf-model",
        dest="serf_model",
        default=None,
        help="Worker model (default: manifest models.serf in .court/config.json)",
    )
    p_dispatch.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_dispatch.set_defaults(func=cmd_dispatch)

    p_coin = sub.add_parser(
        "coin",
        help="Dispatch Master of Coin into a Quest's worktree via Kilo CLI to audit Tribute",
    )
    p_coin.add_argument("quest_id", help="Quest ID to audit (must be in TRIBUTE_READY unless --force)")
    p_coin.add_argument("--model", default=None, help="Model for Master of Coin (default: manifest models.master_of_coin in .court/config.json)")
    p_coin.add_argument("--force", action="store_true", help="Audit even if not currently in TRIBUTE_READY")
    p_coin.add_argument("--wait", action="store_true", help="Run synchronously and wait for completion instead of background")
    p_coin.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_coin.set_defaults(func=cmd_coin)

    p_goad = sub.add_parser(
        "goad",
        help="Goad an active or stalled Serf session in a worktree via Kilo CLI",
    )
    p_goad.add_argument("quest_id", help="Quest ID to goad")
    p_goad.add_argument("--model", default=None, help="Model override for goad")
    p_goad.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_goad.set_defaults(func=cmd_goad)

    p_dispatch_complete = sub.add_parser(
        "dispatch-complete",
        help="Composite: record branch/worktree/serf_session_id/serf_model, advance DISPATCHED -> WORKING, print levy reminder — one command, one commit (Q183)",
    )
    p_dispatch_complete.add_argument("quest_id")
    p_dispatch_complete.add_argument("--session-id", required=True, dest="session_id", help="Serf session id returned by the spawn step")
    p_dispatch_complete.add_argument("--branch", required=True, help="Canonical Quest branch (slash hierarchy)")
    p_dispatch_complete.add_argument("--worktree", required=True, help="Quest worktree path returned by the spawn step")
    p_dispatch_complete.add_argument(
        "--model", "--serf-model",
        dest="serf_model",
        default=None,
        help="Serf model (default: manifest models.serf in .court/config.json; --model and --serf-model are aliases)",
    )
    p_dispatch_complete.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_dispatch_complete.set_defaults(func=cmd_dispatch_complete)

    p_artist = sub.add_parser(
        "artist",
        help="Spawn or prepare a dedicated Court Artist session with runserver for interactive UI review",
    )
    p_artist.add_argument("quest_id", help="Quest ID (e.g. Q196)")
    p_artist.add_argument(
        "--model",
        default=None,
        help="Model for Court Artist (default: manifest models.artist in .court/config.json)",
    )
    p_artist.add_argument(
        "--provider",
        default=None,
        help="Provider for Court Artist model (default: manifest models.artist_provider in .court/config.json)",
    )
    p_artist.add_argument("--port", type=int, help="Override worktree runserver port")
    p_artist.add_argument("--no-server", action="store_true", help="Skip starting the worktree dev server")
    p_artist.add_argument("--no-commit", action="store_true", help="Do not autocommit quest updates")
    p_artist.add_argument("--prompt-only", action="store_true", help="Print only the rendered Court Artist prompt")
    p_artist.add_argument("--json", action="store_true", help="Output JSON format for scripts (spawn payload uses the unified Kilo CLI path)")
    p_artist.set_defaults(func=cmd_artist)

    p_show = sub.add_parser("show", help="Print a Quest/Epic's full markdown or extracted section")
    p_show.add_argument("quest_id", help="Quest ID (e.g. Q182)")
    p_show.add_argument(
        "--section",
        default=None,
        help="Extract and print a specific subsection (e.g. ballad, tribute, tally, penance, opinion, survey, map, dangers, plot)",
    )
    p_show.set_defaults(func=cmd_show)

    p_list = sub.add_parser("list", help="List Quests/Epics")
    add_quest_selector(p_list, batchable=True, filters=("status", "app", "epic"))
    p_list.set_defaults(func=cmd_list)

    p_status = sub.add_parser("status", help="Full court dashboard with live worktree task progress and git states")
    add_quest_selector(p_status, batchable=True, filters=("status", "app", "epic"))
    p_status.add_argument("--tree", action="store_true", help="Display hierarchical quest tree dashboard")
    p_status.add_argument("--sync", action="store_true", help="Auto-sync tribute and task progress from active worktrees")
    p_status.add_argument("--json", action="store_true", help="Output dashboard in JSON format")
    p_status.add_argument("--scouts", action="store_true", help="Show only Scout spikes / Investigation-lane records")
    p_status.set_defaults(func=cmd_status)

    p_tree = sub.add_parser("tree", help="Display hierarchical Quest and Epic tree")
    p_tree.add_argument("--include-archived", dest="include_archived", action="store_true", help="include archived Quests and Epics")
    p_tree.set_defaults(func=cmd_tree)

    p_advance = sub.add_parser("advance", help="Change a Quest's pipeline stage (batchable: one ID, a comma-list, or a --status/--app/--epic filter)")
    add_quest_selector(p_advance, batchable=True, filters=("status", "app", "epic"))
    p_advance.add_argument("status", choices=STATUSES, help="Target pipeline stage")
    p_advance.add_argument("--note", default="")
    p_advance.add_argument("--force", action="store_true", help="Bypass a failed compliance/merge check when advancing to GATE or READY_TO_RAZE")
    p_advance.add_argument(
        "--verified-commit", dest="verified_commit", default=None,
        help="Required alongside --force when overriding an unmerged READY_TO_RAZE check: "
             "the commit SHA you claim is actually merged. Independently re-verified against "
             "castle via `git merge-base --is-ancestor` — the override is refused if it isn't.",
    )
    p_advance.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_advance.set_defaults(func=cmd_advance)

    p_log = sub.add_parser("log", help="Append a history note without changing status")
    p_log.add_argument("quest_id")
    p_log.add_argument("note")
    p_log.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_log.set_defaults(func=cmd_log)

    p_sync = sub.add_parser("sync", help="Pull branch-tip quest ledger (charter + events) onto the main checkout — branch-wins union; true edit wars refuse")
    p_sync.add_argument("quest_ids", nargs="*", help="Quest ID(s), or empty with --all")
    p_sync.add_argument("--all", action="store_true", help="Sync every quest in the main checkout's ledger")
    p_sync.add_argument("--base", default="castle", help="Base branch for merge-base classification (default: castle)")
    p_sync.set_defaults(func=cmd_sync)

    p_commute = sub.add_parser("commute", help="Record a post-deployment commutation as completed (appends a dated entry to the Quest's Cogship Log)")
    p_commute.add_argument("quest_id")
    p_commute.add_argument("--note", "--content", dest="note", default="", help="What was executed to complete the commutation")
    p_commute.add_argument("--file", default=None, help="Read the completion note from a file")
    p_commute.add_argument("--force", action="store_true", help="Append another completion entry even if one is already logged")
    p_commute.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_commute.set_defaults(func=cmd_commute)

    p_pillory = sub.add_parser(
        "pillory",
        aliases=["punish"],
        help="Send a Quest to the pillory: freeze it as PUNISHED with Decrees for a chartered successor (unless proof of landing is found)",
    )
    p_pillory.add_argument("quest_id")
    p_pillory.add_argument("--reason", default="", help="Why the audit was rejected")
    p_pillory.add_argument("--decrees", default="", help="What the successor Quest must keep/discard/reuse")
    p_pillory.add_argument("--successor", default="", help="Successor Quest id, if already chartered")
    p_pillory.set_defaults(func=cmd_pillory)

    p_field = sub.add_parser("set-field", help="Set a frontmatter field")
    p_field.add_argument("quest_id")
    p_field.add_argument("field")
    p_field.add_argument("value")
    p_field.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_field.set_defaults(func=cmd_set_field)

    p_model = sub.add_parser("model", help="Print the resolved model for a role (from .court/config.json)")
    p_model.add_argument("role", help="Role: serf, master_of_coin, gatekeeper, artist, steward, scout")
    p_model.set_defaults(func=cmd_model)

    p_section = sub.add_parser("set-section", help="Replace/append a body section")
    p_section.add_argument("quest_id")
    p_section.add_argument("section")
    p_section.add_argument("--content", default=None)
    p_section.add_argument("--file", default=None)
    p_section.add_argument("--append", action="store_true")
    p_section.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_section.set_defaults(func=cmd_set_section)

    p_verify = sub.add_parser("verify", help="Run a deterministic worktree/test check")
    p_verify.add_argument("quest_id")
    p_verify.add_argument("--test-cmd", default=None)
    p_verify.add_argument("--timeout", type=int, default=600)
    p_verify.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_verify.set_defaults(func=cmd_verify)

    p_verify_merged = sub.add_parser("verify-merged", help="Verify if Quest branch is merged into gatehouse and castle")
    p_verify_merged.add_argument("quest_id", nargs="?", default=None, help="Quest ID, branch name, or worktree path")
    p_verify_merged.add_argument("--include-archived", dest="include_archived", action="store_true", help="Also verify archived Quests, not just active ones")
    p_verify_merged.add_argument("--status", choices=STATUSES, help="Filter by Quest status when --all is used")
    p_verify_merged.add_argument("--target-ref", default="gatehouse", help="Target branch to verify against (default: gatehouse)")
    p_verify_merged.add_argument("--base-ref", default="castle", help="Base branch to verify against (default: castle)")
    p_verify_merged.add_argument("--sync", action="store_true", help="Fast-forward worktree to castle if merged")
    p_verify_merged.add_argument("--json", action="store_true", help="Output JSON")
    p_verify_merged.add_argument("--strict", action="store_true", help="Exit with non-zero code if not cleanly merged")
    p_verify_merged.set_defaults(func=cmd_verify_merged)

    p_raze = sub.add_parser("raze", help="Raze Quest(s): verify merge, sync diffs to castle (ahead: 0, behind: 0), queue for Ashes")
    add_quest_selector(p_raze, batchable=True, filters=("status",))
    p_raze.add_argument("--archive-pruned", dest="archive_pruned", action="store_true", default=True, help="Auto-archive already-pruned quests (default: on)")
    p_raze.add_argument("--no-archive-pruned", dest="archive_pruned", action="store_false", help="Do not auto-archive already-pruned quests")
    p_raze.add_argument("--allow-forced", dest="allow_forced", action="store_true", help="Explicit override: raze a FORCED-promotion quest even if its code-presence audit fails")
    p_raze.set_defaults(func=cmd_raze)

    p_teardown = sub.add_parser("teardown-list", help="List worktrees ready for M'Lord to prune")
    p_teardown.set_defaults(func=cmd_teardown_list)

    p_ui = sub.add_parser("ui", help="Serve the localhost dev console (zero dependencies)")
    p_ui.add_argument("--port", type=int, default=8300)
    p_ui.add_argument("--open", action="store_true", help="Open the browser on start")
    p_ui.set_defaults(func=cmd_ui)

    p_fork_teardown = sub.add_parser(
        "fork-teardown-list",
        help="List Master of Coin / Gatekeeper fork worktrees ready to move+stop, or needing a re-prompt",
    )
    p_fork_teardown.add_argument("--json", action="store_true", help="Output JSON")
    p_fork_teardown.set_defaults(func=cmd_fork_teardown_list)

    p_archive = sub.add_parser("archive", help="Move a Quest/Epic file into .court/archive/")
    p_archive.add_argument("quest_id")
    p_archive.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_archive.set_defaults(func=cmd_archive)

    p_rollup = sub.add_parser("rollup", help="Extract and roll up specific tribute sections across Quests")
    add_quest_selector(p_rollup, batchable=True, filters=("status", "app", "epic", "cogship"))
    p_rollup.add_argument(
        "--section",
        required=True,
        choices=[
            # Serf Tribute pillars
            "ballad", "tribute", "tally", "penance", "audience", "opinion",
            # Scout Report pillars (5-part: Survey, Map, Dangers, Tribute, Plot)
            "survey", "map", "dangers", "plot",
        ],
        help="Tribute subsection to extract (serf: ballad/tribute/tally/penance/audience/opinion; scout: survey/map/dangers/tribute/plot)",
    )
    p_rollup.set_defaults(func=cmd_rollup)

    p_tally = sub.add_parser("tally", help="Extract and summarize production verification runbooks and UI paths across Quests (/tally)")
    add_quest_selector(p_tally, batchable=True, filters=("status", "app", "epic", "cogship"))
    p_tally.set_defaults(func=cmd_tally)

    p_collect = sub.add_parser("collect", help="Pack Master-of-Coin-approved Quests into a Cog Ship convoy: audit, stamp, batch-advance to GATE, print the Gatekeeper NEXT STEPS")
    add_quest_selector(p_collect, batchable=True, filters=("status", "app", "epic"))
    p_collect.add_argument("--cogship", default=None, help="Existing Cog Ship ID to stamp (e.g. cogship-002); omit or pass 'new' to allocate the next id")
    p_collect.add_argument("--base", default="castle", help="Base branch for the compliance audit (default: castle)")
    p_collect.add_argument("--skip-ui-review", action="store_true", help="Bypass pending UI review check when packing into Cog Ship")
    p_collect.add_argument("--hotfix", action="store_true", help="Allow hotfix fast-track inline audit and pack")
    p_collect.add_argument("--standup", action="store_true", help="Automatically stand up the Gatekeeper worktree and session via Kilo CLI")
    p_collect.add_argument("--force", action="store_true", help="Force packing even if checks warn/fail")
    p_collect.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_collect.set_defaults(func=cmd_collect)

    p_atelier = sub.add_parser(
        "atelier",
        help="Royal UI Convoy Atelier: roll up UI-review-pending Quests into one Cog Ship, merge their branches pre-integration-test, and spawn the Court Artist on the merged (untested) branch",
    )
    add_quest_selector(p_atelier, batchable=True, filters=("status", "app", "epic"))
    p_atelier.add_argument("--cogship", default=None, help="Existing Cog Ship ID to stamp (e.g. cogship-002); omit to allocate the next id")
    p_atelier.add_argument("--base", default="castle", help="Base branch the convoy worktree is cut from (default: castle)")
    p_atelier.add_argument("--model", default=None, help="Court Artist model override (default from .court/config.json models.artist)")
    p_atelier.add_argument("--provider", default=None, help="Court Artist provider override")
    p_atelier.add_argument("--port", type=int, help="Override worktree runserver port")
    p_atelier.add_argument("--no-server", action="store_true", help="Skip starting the convoy dev server")
    p_atelier.add_argument("--prompt-only", action="store_true", help="Print only the rendered convoy Court Artist prompt")
    p_atelier.add_argument("--json", action="store_true", help="Output JSON format for scripts (spawn payload uses the unified Kilo CLI path)")
    p_atelier.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_atelier.set_defaults(func=cmd_atelier)

    p_studio = sub.add_parser(
        "studio",
        help="Deterministic Multi-Quest Combined Studio (Q-2): cut an artist-studio worktree from "
             "castle tip, merge N Quest branches with the established conflict policy (charter "
             "paperwork branch-wins; code overlap disclosed-union), start the freshness-gated "
             "runserver, and record the studio + session in the ledger",
    )
    add_quest_selector(p_studio, batchable=True, filters=("status", "app", "epic", "any-status"))
    p_studio.add_argument("--base", default="castle", help="Base branch the studio worktree is cut from (default: castle)")
    p_studio.add_argument("--branch", default=None, help="Studio branch override (default: artist/<ids>-ui-studio; must start with artist/)")
    p_studio.add_argument("--model", default=None, help="Court Artist model override (default from .court/config.json models.artist)")
    p_studio.add_argument("--provider", default=None, help="Court Artist provider override")
    p_studio.add_argument("--port", type=int, help="Override worktree runserver port")
    p_studio.add_argument("--no-server", action="store_true", help="Skip starting the studio dev server (and the freshness gate)")
    p_studio.add_argument("--standup", action="store_true", help="Spawn the Court Artist session via Kilo CLI (Branch B fallback) and record it on every studio Quest")
    p_studio.add_argument("--sync-back", action="store_true", help="Post-sign-off: merge the studio branch back into each selected Quest's own worktree branch (no auto-conflict-resolution). Guarded: base-drift + convoy-race. Superseded by --close for the full lifecycle")
    p_studio.add_argument("--close", action="store_true", help="Close-out lifecycle: five guards (sign-off proof, base-drift, convoy-race, labeled cherry-pick extraction, conflict->artist union brief), close-out manifest, gated teardown")
    p_studio.add_argument("--signoff", default=None, metavar="NOTE", help="--close: write a dated 'studio sign-off' ledger stamp with NOTE at invocation (else a pre-existing stamp is required)")
    p_studio.add_argument("--force-union", action="store_true", help="--close/--sync-back: override the base-drift refusal (drifted-base merges replay stale lines; know what you are doing)")
    p_studio.add_argument("--drift-threshold", type=int, default=None, metavar="N", help="--close/--sync-back: base-drift threshold override (default: studio.close_max_base_drift, else 100)")
    p_studio.add_argument("--override-manifest", action="store_true", help="--close: unlock teardown despite a not-all-green manifest (explicit override)")
    p_studio.add_argument("--skip-teardown", action="store_true", help="--close: stop after the manifest; leave the studio worktree/server/session alive")
    p_studio.add_argument("--artist-session", action="store_true", help="--close: stand up a dedicated artist session in the live studio worktree for union-pending quests")
    p_studio.add_argument("--prompt-only", action="store_true", help="Print only the rendered studio Court Artist prompt")
    p_studio.add_argument("--json", action="store_true", help="Output JSON format for scripts (spawn payload uses the unified Kilo CLI path)")
    p_studio.add_argument("--no-commit", action="store_true", help="Do not autocommit ledger changes")
    p_studio.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not start/adopt the shared studio browser for this studio",
    )
    p_studio.set_defaults(func=cmd_studio)

    p_browser = sub.add_parser(
        "browser",
        help="Shared studio browser: one headed Chromium with a persistent profile that the "
             "Court Artist attaches to over CDP (chrome-devtools MCP) and the annotation "
             "overlay is pinned into",
    )
    browser_sub = p_browser.add_subparsers(dest="browser_command")
    p_browser.set_defaults(func=lambda _args: p_browser.print_help() or sys.exit(2))

    p_browser_start = browser_sub.add_parser(
        "start",
        help="Start (or adopt the already-running) shared studio browser and its annotation injector",
    )
    p_browser_start.add_argument("--headless", action="store_true", help="Run headless (probe/testing mode)")
    p_browser_start.add_argument("--port", type=int, default=None, help="CDP port override (default: 9335 or first free port)")
    p_browser_start.add_argument(
        "--annotate",
        default=None,
        metavar="WORKTREE",
        help="Worktree whose annotation notes the injector serves (pins the overlay script into every page)",
    )
    p_browser_start.set_defaults(func=cmd_browser_start)

    p_browser_status = browser_sub.add_parser("status", help="Show shared browser state, liveness, and CDP probe result")
    p_browser_status.add_argument("--json", action="store_true", help="Output JSON format")
    p_browser_status.set_defaults(func=cmd_browser_status)

    p_browser_stop = browser_sub.add_parser("stop", help="Stop the shared browser and injector recorded in the state file (only those)")
    p_browser_stop.set_defaults(func=cmd_browser_stop)

    p_browser_inject = browser_sub.add_parser(
        "_inject_worker",
        help="internal: long-running annotation injector loop (spawned by `browser start`)",
        description="Internal: long-running annotation injector loop (spawned by `browser start`)",
    )
    p_browser_inject.add_argument("worktree", help="Worktree whose studio annotations the injector serves")
    p_browser_inject.set_defaults(func=cmd_browser_inject_worker)

    p_artist_say = sub.add_parser(
        "artist-say",
        help="Deliver M'Lord's instruction to an existing Court Artist session (kilo run --agent artist --session <artist_session_id>)",
    )
    p_artist_say.add_argument("quest_id", help="Quest ID with a recorded artist_session_id (e.g. Q196)")
    p_artist_say.add_argument("instruction", help="The instruction to deliver to the artist session")
    p_artist_say.set_defaults(func=cmd_artist_say)

    p_stamp = sub.add_parser("stamp", help="Stamp Quests onto a Cog Ship convoy id (allocates next cogship-NNN unless --cogship given)")
    p_stamp.add_argument("quest_ids", help="Comma-separated Quest IDs (e.g. Q101,Q102,Q105)")
    p_stamp.add_argument("--cogship", default=None, help="Existing Cog Ship ID to stamp (e.g. cogship-002); omit or pass 'new' to allocate the next id")
    p_stamp.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_stamp.add_argument("--force", action="store_true", help="Allow re-stamping quests already stamped on another cogship (duplicate-convoy guard override)")
    p_stamp.set_defaults(func=cmd_stamp)

    p_edict = sub.add_parser("edict", help="View or add royal edicts and strategic priorities")
    p_edict.add_argument("content", nargs="?", default=None, help="Edict text to record")
    p_edict.add_argument("--file", default=None, help="Read edict from file")
    p_edict.add_argument("--append", action="store_true", default=True, help="Append to existing edicts")
    p_edict.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_edict.set_defaults(func=cmd_edict)

    p_ship = sub.add_parser("ship", help="Generate full Cog Ship deployment convoy summary (/bard, /coffers, /tally, /atone, /murmur)")
    add_quest_selector(p_ship, batchable=True, filters=("status", "app", "epic", "cogship"))
    p_ship.add_argument("--base", default="main", help="Target production branch (default: main)")
    p_ship.add_argument("--head", default="castle", help="Source staging branch (default: castle)")
    p_ship.add_argument(
        "--confirm",
        action="store_true",
        help="Execute the deployment: merge castle into main and push main to origin (triggering autodeploy)",
    )
    p_ship.add_argument(
        "--dry-run",
        action="store_true",
        help="With --confirm: run preflight safety checks only; no merge/push/deploy is executed",
    )
    p_ship.add_argument(
        "--fly-app",
        action="append",
        default=None,
        choices=sorted(FLY_CONFIGS),
        metavar="{web,worker,worker-heavy}",
        help="Fly app(s) to deploy (repeatable; default: all apps with a config in the repo root)",
    )
    p_ship.set_defaults(func=cmd_ship)

    p_audit = sub.add_parser("audit", help="Audit Quest tribute presence, gaps, and protocol compliance")
    add_quest_selector(p_audit, batchable=True, filters=("status", "app", "epic"))
    p_audit.add_argument("--base", default="castle", help="Base branch to check drift against (default: castle)")
    p_audit.add_argument("--json", action="store_true", help="Output JSON format")
    p_audit.set_defaults(func=cmd_audit)

    p_levy = sub.add_parser("levy", help="Deterministic worktree triage, tribute synchronization, and review advancement")
    add_quest_selector(p_levy, batchable=True, filters=("status", "app", "epic", "any-status"))
    p_levy.add_argument("--base", default="castle", help="Base branch to check drift against (default: castle)")
    p_levy.add_argument("--advance", action="store_true", help="Auto-advance compliant Quests to TRIBUTE_READY")
    p_levy.add_argument("--no-sync", dest="sync", action="store_false", default=True, help="Skip syncing tribute from worktree")
    p_levy.add_argument("--no-rebase", dest="rebase", action="store_false", default=True, help="Skip the mechanical pre-flight `git merge <base>` sweep (see `court rebase --help`)")
    p_levy.add_argument("--json", action="store_true", help="Output JSON format")
    p_levy.set_defaults(func=cmd_levy)

    p_rebase = sub.add_parser("rebase", help="Mechanically converge worktrees onto --base (default castle) via `git merge` — zero agent turns")
    add_quest_selector(p_rebase, batchable=True, filters=("status", "app", "epic"))
    p_rebase.add_argument("--base", default="castle", help="Base branch to merge into each worktree (default: castle)")
    p_rebase.add_argument("--dry-run", dest="dry_run", action="store_true", help="Report current ahead/behind only; do not merge")
    p_rebase.add_argument("--json", action="store_true", help="Output JSON format")
    p_rebase.set_defaults(func=cmd_rebase)

    p_runsuite = sub.add_parser(
        "runsuite",
        help="Run the unified integration suite via the ENGINE (long timeout, immune to agent shell limits) and stamp durable proof for the READY_TO_RAZE gate",
    )
    p_runsuite.add_argument("quest_id", nargs="?", default=None, help="Quest ID for a size-1 convoy (runs in its own worktree)")
    p_runsuite.add_argument("--cogship", default=None, help="Cog Ship ID the suite run is stamped against (e.g. cogship-082)")
    p_runsuite.add_argument("--dir", default=None, help="Worktree to run in (REQUIRED for convoys: the ephemeral gatehouse worktree)")
    p_runsuite.add_argument("--command", default=None, help="Override suite command (default: manifest suite.command in .court/config.json, then pytest/Django auto-detect)")
    p_runsuite.add_argument("--timeout", type=int, default=1800, help="Engine timeout in seconds (default 1800 — an agent's 120s shell cap can never run a real suite)")
    p_runsuite.add_argument("--json", action="store_true", help="Output JSON format")
    p_runsuite.set_defaults(func=cmd_runsuite)

    p_watch = sub.add_parser(
        "watch",
        help="Detached continuation watchdog: when a single-turn worker run ends mid-task, re-prompt the SAME session (generalized goad; spawned automatically beside every engine worker spawn)",
    )
    p_watch.add_argument("--pid", type=int, default=None, help="Worker process id to wait on (optional; no pid means poll the quest state once)")
    p_watch.add_argument("--dir", default=None, help="Worktree the worker runs in (watch state, logs, and ledger reads resolve here)")
    p_watch.add_argument("--agent", required=True, help="Worker role (serf | scout | gatekeeper | master_of_coin)")
    p_watch.add_argument("--model", default=None, help="Model for continuation turns (defaults to the worker's recorded model resolution)")
    p_watch.add_argument("--session", default="", help="Worker session id to re-prompt (resolved from kilo.db when omitted)")
    p_watch.add_argument("--quest", default="", help="Comma-separated quest ids bound to the run (durable completion signal)")
    p_watch.add_argument("--task-file", dest="task_file", default="TASK.md", help="Task file under .kilo/ whose text seeds the continuation prompt (default TASK.md)")
    p_watch.add_argument("--max-attempts", dest="max_attempts", type=int, default=WATCH_MAX_ATTEMPTS, help="Maximum total turns (default 3)")
    p_watch.add_argument("--attempt", type=int, default=1, help="Starting attempt number (internal)")
    p_watch.add_argument("--exclude", default="", help="Comma-separated pre-existing session ids to exclude when resolving the worker session")
    p_watch.add_argument("--session-resolve-seconds", dest="session_resolve_seconds", type=int, default=6, help="How long to poll kilo.db for the worker session id when only a pid is known")
    p_watch.add_argument("--wall-cap", type=int, default=WATCH_WALL_CAP_SECONDS, help="Hard wall-clock cap for the whole watch (seconds)")
    p_watch.set_defaults(func=cmd_watch)

    p_monitor = sub.add_parser(
        "monitor",
        help="One-shot cron task: classified `court sync --all` status roll-up + lifecycle teardown sweep (safe to schedule; exits 0)",
    )
    p_monitor.set_defaults(func=cmd_monitor)

    p_verify_manifest = sub.add_parser(
        "verify-manifest",
        help="Post-promotion integrity: assert each manifest Quest's branch TIP is an ancestor of the trunk AND carries production content (would have caught cogship-076/077)",
    )
    p_verify_manifest.add_argument("--cogship", default=None, help="Verify every Quest stamped onto this Cog Ship ID")
    p_verify_manifest.add_argument("--trunk", default="castle", help="Promoted trunk branch to verify against (default: castle)")
    add_quest_selector(p_verify_manifest, batchable=True)
    p_verify_manifest.set_defaults(func=cmd_verify_manifest)

    p_diff = sub.add_parser("diff", help="Display structured worktree diff and file statistics vs base")
    p_diff.add_argument("quest_id", help="Quest ID to diff")
    p_diff.add_argument("--base", default="castle", help="Base branch/ref to diff against (default: castle)")
    p_diff.add_argument("--stat", action="store_true", help="Show diffstat summary only")
    p_diff.add_argument("--numstat", action="store_true", help="Show per-file additions/deletions")
    p_diff.add_argument("--patch", "--full", dest="patch", action="store_true", help="Show full patch")
    p_diff.add_argument("--json", action="store_true", help="Output JSON format")
    p_diff.set_defaults(func=cmd_diff)

    p_wt_doc = sub.add_parser("worktree-doc", help="Inspect, diff, or sync the task document from a Quest's active worktree")
    p_wt_doc.add_argument("quest_id", help="Quest ID (e.g. Q079)")
    p_wt_doc.add_argument("--path", action="store_true", help="Print absolute path of worktree task document")
    p_wt_doc.add_argument("--diff", action="store_true", help="Show diff between castle ledger doc and worktree doc")
    p_wt_doc.add_argument("--sync", action="store_true", help="Sync tribute and frontmatter from worktree doc to castle")
    p_wt_doc.add_argument("--json", action="store_true", help="Output JSON format")
    p_wt_doc.set_defaults(func=cmd_worktree_doc)

    p_timber = sub.add_parser("timber", help="List physical git worktrees mapped to Agent Manager sections and Court Quests")
    p_timber.add_argument("--json", action="store_true", help="Output JSON format")
    p_timber.set_defaults(func=cmd_timber)

    p_init = sub.add_parser("init", help="initialize .court/, .kilo/commands/, .kilo/prompts/, and AGENTS.md in the repository")
    p_init.add_argument("--force", action="store_true", help="overwrite existing files")
    p_init.set_defaults(func=cmd_init)

    p_update = sub.add_parser("update", help="Update .court/engine/, templates, commands, prompts, and agents from Kilo Castle")
    p_update.set_defaults(func=cmd_update)

    p_ward = sub.add_parser("ward", help="Warden's hunting-grounds patrol: last survey, pending Warden Reports, and realm compliance health")
    p_ward.add_argument("--base", default="castle", help="Base branch to check drift against (default: castle)")
    p_ward.add_argument("--json", action="store_true", help="Output JSON format")
    p_ward.set_defaults(func=cmd_ward)

    for cmd_alias in ("fix-branches", "realign-branches"):
        p_fix = sub.add_parser(cmd_alias, help="Scan and repair flat branches to forward-slash hierarchy")
        p_fix.add_argument("--dry-run", action="store_true", help="Preview branch renames without executing")
        p_fix.add_argument("--current-worktree", action="store_true", help="Realign branch in the current worktree directory")
        p_fix.add_argument("--no-sync", dest="sync", action="store_false", default=True, help="Skip syncing baseline with castle")
        p_fix.add_argument("--json", action="store_true", help="Output JSON format")
        p_fix.set_defaults(func=cmd_fix_branches)

    return p


# Castle-contention fix (2026-09-13): mutating commands serialize on the
# court-wide write lock so concurrent roles (Steward, Master of Coin,
# Gatekeeper, raze/commute) queue cleanly instead of colliding on the shared
# checkout's index / dirty tree. Read-only reporting commands never take it.
_READ_ONLY_COMMANDS = {
    "status", "show", "list", "tally", "ward", "ship", "diff", "timber",
    "model", "verify-merged", "verify-manifest", "runsuite", "browser",
    # `watch` only observes durable state and re-prompts sessions; it never
    # commits, so it must not serialize behind the write lock (a watchdog
    # blocking on a busy castle would delay continuations). `monitor` runs
    # sync as its own subprocess which takes the lock itself.
    "watch", "monitor",
    # `ui` is a long-lived server: it must never hold the court-wide write
    # lock for its whole lifetime (that deadlocked every root-level mutation
    # until the server died). Its in-process court ops shell out as
    # short-lived subprocesses which serialize on the lock themselves.
    "ui",
}


def main(argv=None):
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    command_name = next((tok for tok in argv if not tok.startswith("-")), "")
    if command_name in _READ_ONLY_COMMANDS or not hasattr(args, "func"):
        try:
            args.func(args)
        except config.CourtConfigError as e:
            # Q455: a missing manifest entry fails loudly and names the fix —
            # role models resolve only from .court/config.json now.
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(2)
        return
    with git_ops.court_write_lock():
        try:
            args.func(args)
        except config.CourtConfigError as e:
            # Q455: a missing manifest entry fails loudly and names the fix —
            # role models resolve only from .court/config.json now.
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    main()
