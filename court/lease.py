"""Worker lease: cross-process single-writer arbitration for engine-spawned
agent workers.

2026-10-06 Q799 incident class: a live dispatch Serf, a manual goad, and a
console "continue" stacked three concurrent agents onto ONE worktree — three
writers racing one branch. The convoy-op op-lock (`acquire_op_lock`,
cogship-040/041/247/248) solved this class for collect/ship; worker spawns
never adopted it. This module is the worker-spawn analog:

- one live holder per (worktree, role) lease at
  `<worktree>/.kilo/watch/<role>.lease.json`
- spawn paths claim BEFORE Popen (a pending claim with a short grace window
  covers the spawn-then-learn-pid gap), then upgrade the claim with the
  worker pid once Popen returns — a racing spawner is refused before it
  ever creates a duplicate worker
- a dead or pid-reused holder is stolen; a live holder refuses the spawn
  (`--force` supersedes: kill the holder tree first)
- log-liveness: a worker's stdout is teed into `.kilo/<role>.log`; the log
  mtime age classifies live vs silent, feeding both the dashboard and the
  watchdog's stall referee (kill a hung worker, then re-prompt its session)

Design limits, stated honestly: a lease holder is verified alive via
os.kill(pid, 0) plus a `ps` command-line check (guards against pid reuse);
neither is airtight on a machine churning through pids, and the `--force`
path is the documented escape hatch for any wedged state. There is no
in-worker heartbeat — a worker that streams nothing is indistinguishable
from a hung one, which is exactly the definition the referee uses.
"""
import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PENDING_GRACE_SECONDS = 90
DEFAULT_STALL_SECONDS = 20 * 60
STALL_THRESHOLDS = {
    "serf": 20 * 60,
    "scout": 30 * 60,
    "gatekeeper": 45 * 60,
    "master_of_coin": 30 * 60,
}
KILL_GRACE_SECONDS = 5.0
# Command-line tokens a genuine worker process carries; a lease pid whose
# `ps` command matches NONE of these was reused by an unrelated process.
_COMMAND_TRUST_TOKENS = ("kilo", "python", "court")


def lease_path(worktree: Path, role: str) -> Path:
    return Path(worktree) / ".kilo" / "watch" / f"{role}.lease.json"


def read_lease(worktree: Path, role: str) -> Optional[dict]:
    try:
        raw = lease_path(worktree, role).read_text(encoding="utf-8")
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid) -> bool:
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


def pid_command(pid: int) -> str:
    """Best-effort `ps` command line for a pid ("" when unobtainable)."""
    try:
        res = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=3,
        )
        if res.returncode == 0:
            return (res.stdout or "").strip()
    except Exception:
        pass
    return ""


def _pid_is_zombie(pid) -> bool:
    """True when the pid is a killed-but-unreaped zombie: os.kill(zombie, 0)
    still succeeds, so a plain pid probe would report a freshly killed worker
    as 'alive' forever (until its parent reaps it — which may be never when
    the parent is itself a detached CLI process)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        res = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True, text=True, timeout=3,
        )
        if res.returncode == 0:
            return (res.stdout or "").strip().startswith("Z")
    except Exception:
        pass
    return False


def _worker_down(pid) -> bool:
    """Dead for kill purposes: reaped-gone, exited-but-unreaped (our own
    child via waitpid), or a ps-confirmed zombie. os.kill(zombie, 0) still
    succeeds, so a plain pid probe alone would wait out the whole kill grace
    against a corpse."""
    if not isinstance(pid, int) or pid <= 0:
        return True
    try:
        wpid, _status = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return True  # our child, exited (and now reaped)
        if wpid == 0:
            return False  # our child, still running
    except ChildProcessError:
        pass  # not our child — fall through to the probes
    except OSError:
        pass
    if not _pid_alive(pid):
        return True
    return _pid_is_zombie(pid)


def _pid_trusted(pid: int) -> bool:
    """True when a live pid still looks like a worker process (guards the
    refusal path against pid reuse). Unverifiable -> assume live (fail
    closed: refusing a spawn is recoverable; double-spawning is not)."""
    cmd = pid_command(pid)
    if not cmd:
        return True
    low = cmd.lower()
    return any(tok in low for tok in _COMMAND_TRUST_TOKENS)


def holder_is_live(payload: dict, now: Optional[float] = None) -> tuple[bool, str]:
    """Is the lease holder still holding? (live, why)."""
    now = time.time() if now is None else now
    if payload.get("pending"):
        started = payload.get("started") or 0
        age = now - float(started or 0)
        if age < PENDING_GRACE_SECONDS:
            return True, f"spawn in flight ({payload.get('holder') or 'unknown'}, {age:.0f}s ago)"
        return False, f"pending claim expired ({age:.0f}s old, pid never recorded)"
    pid = payload.get("pid")
    if not _pid_alive(pid):
        return False, f"pid {pid} gone"
    if _pid_is_zombie(pid):
        return False, f"pid {pid} zombie (killed, unreaped)"
    if not _pid_trusted(pid):
        return False, f"pid {pid} reused by another process"
    return True, f"pid {pid} live"


def ensure_self_ignored(worktree: Path) -> None:
    """Keep engine runtime state (.kilo/watch/*) out of `git status` dirty
    counts — the Q799 dashboard flagged .kilo/watch/serf.json as a dirty work
    file, a false alarm on every watchdog write."""
    gi = Path(worktree) / ".kilo" / "watch" / ".gitignore"
    try:
        if gi.parent.is_dir() and not gi.exists():
            gi.write_text("*\n", encoding="utf-8")
    except OSError:
        pass


def _write_lease(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_self_ignored(path.parent.parent)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _base_payload(holder: str, model: str, pending: bool, now: float) -> dict:
    return {
        "v": 1,
        "pending": bool(pending),
        "pid": 0,
        "pgid": None,
        "session_id": "",
        "watchdog_pid": None,
        "holder": holder or "unknown",
        "model": model or "",
        "started": now,
        "started_iso": _now_iso(),
        "heartbeat": now,
        "heartbeat_iso": _now_iso(),
    }


def claim(
    worktree: Path,
    role: str,
    *,
    holder: str = "",
    pid: int = 0,
    pgid: Optional[int] = None,
    session_id: str = "",
    model: str = "",
    pending: bool = False,
    force: bool = False,
    now: Optional[float] = None,
) -> tuple[bool, str, Optional[dict]]:
    """Atomically claim the single-writer worker lease for (worktree, role).

    pending=True writes a pid-less claim used between "decided to spawn" and
    "Popen returned" so a racing spawner is refused before creating a
    duplicate worker; the owner finalizes via update() once the pid is known.
    Returns (ok, reason, previous_payload). reason: claimed | stolen-stale |
    stolen-expired-pending | refused-live | refused-force | contended | error.
    """
    now_ts = time.time() if now is None else now
    lp = lease_path(worktree, role)
    lp.parent.mkdir(parents=True, exist_ok=True)
    ensure_self_ignored(worktree)
    previous = read_lease(worktree, role)
    reason = "claimed"
    for _attempt in range(3):
        previous = read_lease(worktree, role)
        if previous:
            live, why = holder_is_live(previous, now=now_ts)
            if live:
                if previous.get("pid") == os.getpid():
                    # Self-held lease (e.g. a sync run re-claiming after an
                    # earlier claim in the same process): ours, no kill.
                    release(worktree, role)
                    reason = "self-reclaimed"
                elif not force:
                    return False, f"refused-live ({why})", previous
                else:
                    killed, kdetail = kill_worker_tree(
                        previous.get("pid"), pgid=previous.get("pgid"), verify=False
                    )
                    if not killed:
                        return False, f"refused-force ({kdetail})", previous
                    release(worktree, role)
                    reason = "force-superseded"
            else:
                release(worktree, role)
                reason = "stolen-stale" if not previous.get("pending") else "stolen-expired-pending"
        try:
            fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue  # lost a steal race; re-read and decide again
        except OSError as e:
            return False, f"error ({e})", previous
        payload = _base_payload(holder, model, pending, now_ts)
        payload.update({"pid": pid, "pgid": pgid, "session_id": session_id})
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(payload, indent=2))
        return True, reason, previous
    return False, "contended", previous


def update(worktree: Path, role: str, **fields) -> Optional[dict]:
    """Owner-only in-place lease rewrite (pending -> final upgrade,
    watchdog adoption, heartbeat refresh). Never creates a lease."""
    payload = read_lease(worktree, role)
    if payload is None:
        return None
    for key, val in fields.items():
        if val is not None:
            payload[key] = val
    payload["pending"] = bool(fields.get("pending", payload.get("pending")))
    payload["heartbeat"] = time.time()
    payload["heartbeat_iso"] = _now_iso()
    _write_lease(lease_path(worktree, role), payload)
    return payload


def adopt(
    worktree: Path,
    role: str,
    *,
    pid: Optional[int] = None,
    pgid: Optional[int] = None,
    session_id: str = "",
    watchdog_pid: Optional[int] = None,
    holder: str = "watch",
) -> tuple[bool, str]:
    """Watchdog startup handoff: record watchdog ownership of the lease.

    Creates the lease when missing (manual `court watch` on a live pid);
    takes over when the recorded holder is dead or is the same pid; refuses
    to clobber a DIFFERENT live holder (an external worker exists — the
    Q799 stack class). Returns (adopted, detail)."""
    payload = read_lease(worktree, role)
    if payload is not None:
        live, why = holder_is_live(payload)
        same_pid = pid is not None and payload.get("pid") == pid
        if live and not same_pid:
            # Includes pending claims: a spawn already in flight must not be
            # adopted over (the spawner will finalize it with the real pid).
            return False, f"lease held by another live worker ({why})"
    if pid is None:
        return False, "no pid to adopt"
    if payload is None:
        claim(worktree, role, holder=holder, pid=pid, pgid=pgid, session_id=session_id)
    update(
        worktree, role,
        pid=pid, pgid=pgid, session_id=session_id,
        watchdog_pid=watchdog_pid, holder=holder, pending=False,
    )
    return True, f"adopted (pid {pid}, watchdog {watchdog_pid})"


def release(worktree: Path, role: str, pid: Optional[int] = None) -> bool:
    """Remove the lease (optionally only when a given pid still holds it)."""
    lp = lease_path(worktree, role)
    if pid is not None:
        payload = read_lease(worktree, role)
        if payload is None or payload.get("pid") != pid:
            return False
    try:
        lp.unlink()
        return True
    except OSError:
        return False


def kill_worker_tree(pid: Optional[int], pgid: Optional[int] = None, verify: bool = True) -> tuple[bool, str]:
    """SIGTERM then SIGKILL a worker process group; (killed, detail).

    verify=True guards against pid reuse: a live pid whose `ps` command
    carries none of the trusted tokens is NOT ours — refuse to kill."""
    if _worker_down(pid):
        return False, f"pid {pid} already gone"
    if verify and not _pid_trusted(pid):
        return False, f"pid {pid} reused by another process; not killing"
    target = pgid if isinstance(pgid, int) and pgid > 1 else pid
    sig_name = "sigterm"
    try:
        if target != pid:
            os.killpg(target, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return False, f"pid {pid} died before signal"
    deadline = time.time() + KILL_GRACE_SECONDS
    while time.time() < deadline and not _worker_down(pid):
        time.sleep(0.1)
    if not _worker_down(pid):
        sig_name = "sigkill"
        try:
            if target != pid:
                os.killpg(target, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        deadline = time.time() + KILL_GRACE_SECONDS
        while time.time() < deadline and not _worker_down(pid):
            time.sleep(0.1)
    if not _worker_down(pid):
        return False, f"pid {pid} survived {sig_name}"
    return True, f"{sig_name} delivered, pid {pid} down"


def stall_threshold(role: str) -> int:
    return STALL_THRESHOLDS.get(role or "", DEFAULT_STALL_SECONDS)


def log_freshness(worktree: Path, role: str, now: Optional[float] = None) -> dict:
    """Classify the worker log's last activity: live (fresh) / silent (older
    than the role's stall threshold) / unknown (no log — no evidence, never
    a kill basis)."""
    now_ts = time.time() if now is None else now
    log = Path(worktree) / ".kilo" / f"{role}.log"
    try:
        age = now_ts - log.stat().st_mtime
    except OSError:
        return {"log": str(log), "exists": False, "age_seconds": None, "verdict": "unknown"}
    verdict = "live" if age <= stall_threshold(role) else "silent"
    return {"log": str(log), "exists": True, "age_seconds": age, "verdict": verdict}


def worker_verdict(worktree: Path, role: str, now: Optional[float] = None) -> dict:
    """Dashboard truth: lease + log freshness -> worker state.

    States: idle (no lease) | live | stalled (pid live, log silent past
    threshold) | watching (turn ended, watchdog alive and owns re-prompting)
    | exited (worker gone, no live watchdog — needs a goad if the quest is
    still active)."""
    now_ts = time.time() if now is None else now
    payload = read_lease(worktree, role)
    fresh = log_freshness(worktree, role, now=now_ts)
    if not payload:
        return {"state": "idle", "role": role, "lease": None, "log": fresh}
    live, why = holder_is_live(payload, now=now_ts)
    if live:
        state = "stalled" if fresh["verdict"] == "silent" else "live"
    else:
        wd = payload.get("watchdog_pid")
        state = "watching" if _pid_alive(wd) else "exited"
    return {
        "state": state,
        "role": role,
        "lease": payload,
        "holder": payload.get("holder"),
        "pid": payload.get("pid"),
        "session_id": payload.get("session_id"),
        "why": why,
        "log": fresh,
    }


def fmt_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def format_badge(verdict: dict) -> str:
    """One-line dashboard badge for a worker verdict ("" when idle)."""
    state = verdict.get("state") or "idle"
    if state == "idle":
        return ""
    fresh = verdict.get("log") or {}
    age = fmt_age(fresh.get("age_seconds"))
    pid = verdict.get("pid")
    if state == "live":
        activity = age if fresh.get("exists") else "activity unknown"
        return f"[worker: live pid {pid}, output {activity} ago]"
    if state == "stalled":
        return f"[worker: STALLED pid {pid}, silent {age}]"
    if state == "watching":
        return "[worker: turn ended — watchdog re-prompting]"
    return f"[worker: exited pid {pid} — stale lease, goad if still active]"
