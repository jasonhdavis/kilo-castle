"""Night's Watch: deterministic fleet supervisor for the Court pipeline.

Why this exists (2026-10-31): the first attempt at overnight supervision was a
pile of LLM `schedule_wakeup` calls. That fails twice over — a wakeup is an
in-memory session timer that dies with its session (the cogship-281 class), and
a reasoning model handed "check back in 15 minutes" almost always answers by
setting ANOTHER wakeup and ending the turn. The result is a spin loop that
burns tokens and advances nothing.

The Night's Watch is the mechanical alternative: a small, token-free Python
daemon that reads the Court's durable state and issues the same deterministic
verbs a human Steward would — `goad`, `coin`, `collect --standup` — under hard
caps. It never writes quest prose and never improvises work.

Scope of authority (deliberately narrow):
  * WORKING/QUESTING + dead worker      -> `goad` (bounded per serf; a spent serf
                                           falls over to a replacement `dispatch`)
  * TRIBUTE_READY + no live MoC         -> `coin`
  * GATE + no live/recorded Gatekeeper  -> `collect --standup` (a PENDING UI
                                           review is escalated, never collected)
  * PLANNED/CHARTERED/DISPATCHED        -> report only (dispatching new work is a
                                           Steward judgment call, not a timer —
                                           the one exception is a bounded
                                           replacement serf for a spent Quest)
  * READY_TO_RAZE                       -> report only (razing is destructive)
  * stale board, nobody running         -> `muster` ONE bounded Steward triage
                                           session (opt-in; see below)

Hard caps exist because this runs unattended on a laptop with limited RAM:
a live-worker ceiling (`--max-workers`), per-tick spawn limit, hourly spawn
budget, per-quest cooldowns, and a worker-recovery budget (`serf_slots` serfs x
`goads_per_serf` goads) after which the Quest is bubbled up to a Steward
instead of being retried forever.

The Steward muster is the one action that can bring up a *judgment* worker. It
is deliberately opt-in (`--enable ...,steward`), and is only reachable when the
watch has genuinely run out of mechanical moves: no live worker anywhere, a
standing blocker or report-only Quest that only a Steward can resolve, and an
unchanged board signature for `steward_stale_after` seconds. It is additionally
bounded by a cooldown and a rolling 24-hour spawn cap, and it never re-prompts:
a muster is one single-turn triage session, not a daemon.

Every action is journaled to
`~/.local/state/kilo-castle/nightwatch/<repo>.jsonl`, and the daemon's state
(cooldowns, attempt counters) lives beside it, never in the tracked tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import handoff
from . import vigils

STATE_VERSION = 1
DEFAULT_INTERVAL = 300
DEFAULT_MAX_WORKERS = 3
DEFAULT_MAX_SPAWNS_PER_TICK = 2
DEFAULT_MAX_SPAWNS_PER_HOUR = 50
DEFAULT_GOAD_COOLDOWN = 20 * 60
DEFAULT_COIN_COOLDOWN = 30 * 60
DEFAULT_GATE_COOLDOWN = 60 * 60
DEFAULT_MAX_GOADS = 3
DEFAULT_MAX_COINS = 3
# A Quest's worker-recovery budget is serf_slots x goads_per_serf before the
# watch stops retrying and bubbles the Quest up to a Steward. The goad ceiling
# is therefore (serf_slots * goads_per_serf); when one serf exhausts its goads
# the watch "falls over to dispatch" a replacement serf instead of goading on.
DEFAULT_GOADS_PER_SERF = 3
DEFAULT_SERF_SLOTS = 2
# A `collect` that packs nothing (UI review PENDING, dirty tree, violations) is
# NOT progress: back the convoy off for this long instead of a bare retry so a
# blocked convoy cannot consume the per-tick action budget forever.
DEFAULT_COLLECT_NOOP_BACKOFF = 60 * 60
DEFAULT_RETRY_BACKOFF = 300
# Steward muster: the last-resort action. The board must be unchanged for
# `steward_stale_after`, with no live worker at all and a standing blocker,
# before the watch brings up one single-turn Steward triage session. A muster
# costs a full reasoning worker, so it is cooldown- and daily-capped and is
# never enabled by the CLI default (opt in with `--enable ...,steward`).
DEFAULT_STEWARD_STALE_AFTER = 45 * 60
DEFAULT_STEWARD_COOLDOWN = 6 * 60 * 60
DEFAULT_MAX_STEWARDS_PER_DAY = 2
# Context thresholds (tokens). `cull_soft` is the advisory cull line at which a
# live watchdog re-prompting a session is flagged (or, with --bloat-cold-start,
# superseded by a fresh goad); `hard_red_zone` is the red zone past which a
# same-session continuation is unacceptable. Resolved from the manifest's
# context.* by the CLI; these are the structural fallbacks.
DEFAULT_CULL_SOFT = 350_000
DEFAULT_HARD_RED_ZONE = 750_000
DEFAULT_STATUS_TIMEOUT = 240
DEFAULT_ACTION_TIMEOUT = 600
COLLECT_TIMEOUT = 900
JOURNAL_MAX_BYTES = 2 * 1024 * 1024

# Worker log silence thresholds, mirroring court.lease.STALL_THRESHOLDS. Kept
# local so the supervisor stays runnable against a repo whose engine version
# differs from this one (pb-app and kilo-castle engines are separate copies).
STALL_THRESHOLDS = {
    "serf": 20 * 60,
    "scout": 30 * 60,
    "gatekeeper": 45 * 60,
    "master_of_coin": 30 * 60,
}

ENABLED_ACTIONS = ("goad", "coin", "collect", "dispatch", "steward")

# A board is only "live" while at least one Quest is in a pre-terminal stage.
# Statuses the watch can move mechanically are excluded from the `steward`
# trigger's blocker test; these are the stages where only a Steward decision
# (dispatch/regate/raze/audience) can move the board.
ACTIVE_STAGES = (
    "PLANNED", "CHARTERED", "DISPATCHED", "WORKING", "QUESTING",
    "TRIBUTE_READY", "GATE", "READY_TO_RAZE",
)
STEWARD_ONLY_STAGES = ("PLANNED", "CHARTERED", "DISPATCHED", "READY_TO_RAZE")


# ---------------------------------------------------------------------------
# Policy / state
# ---------------------------------------------------------------------------


@dataclass
class Policy:
    max_workers: int = DEFAULT_MAX_WORKERS
    max_spawns_per_tick: int = DEFAULT_MAX_SPAWNS_PER_TICK
    max_spawns_per_hour: int = DEFAULT_MAX_SPAWNS_PER_HOUR
    goad_cooldown: int = DEFAULT_GOAD_COOLDOWN
    coin_cooldown: int = DEFAULT_COIN_COOLDOWN
    gate_cooldown: int = DEFAULT_GATE_COOLDOWN
    max_goads: int = DEFAULT_MAX_GOADS
    max_coins: int = DEFAULT_MAX_COINS
    # Worker-recovery budget: a Quest gets `serf_slots` serfs, each with
    # `goads_per_serf` goads, before bubbling up to a Steward. When serf N
    # exhausts its goads the watch dispatches a replacement (serf N+1) rather
    # than goading the same dead session forever.
    goads_per_serf: int = DEFAULT_GOADS_PER_SERF
    serf_slots: int = DEFAULT_SERF_SLOTS
    collect_noop_backoff: int = DEFAULT_COLLECT_NOOP_BACKOFF
    retry_backoff: int = DEFAULT_RETRY_BACKOFF
    # Steward muster (last resort). See DEFAULT_STEWARD_* above.
    steward_stale_after: int = DEFAULT_STEWARD_STALE_AFTER
    steward_cooldown: int = DEFAULT_STEWARD_COOLDOWN
    max_stewards_per_day: int = DEFAULT_MAX_STEWARDS_PER_DAY
    cull_soft: int = DEFAULT_CULL_SOFT
    hard_red_zone: int = DEFAULT_HARD_RED_ZONE
    bloat_cold_start: bool = False
    # A dispatch is only ever reachable through an explicitly registered
    # vigil (which carries its own reason, expiry, and fire cap) — the watch
    # never invents new work on its own.
    allow_dispatch: bool = True
    enabled: tuple = ENABLED_ACTIONS
    allow_force_goad: bool = False
    status_timeout: int = DEFAULT_STATUS_TIMEOUT

    def allows(self, kind: str) -> bool:
        return kind in self.enabled

    def goad_budget(self) -> int:
        """Total worker-recovery goads before a Quest bubbles up to a Steward
        (`serf_slots` serfs x `goads_per_serf` goads)."""
        return max(1, self.serf_slots) * max(1, self.goads_per_serf)


def state_dir(repo: Path) -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) / "kilo-castle" / "nightwatch" if base else (
        Path.home() / ".local" / "state" / "kilo-castle" / "nightwatch"
    )
    return root


def repo_slug(repo: Path) -> str:
    """Stable per-repo key. Builtin hash() is salted per process, which
    silently gave every invocation its own state file and lock (caught the
    first minute the daemon ran: `--status` reported "not running" while the
    loop was alive under a different slug)."""
    name = repo.resolve().name or "repo"
    digest = hashlib.sha1(str(repo.resolve()).encode("utf-8")).hexdigest()[:6]
    return f"{name}-{digest}"


def state_path(repo: Path) -> Path:
    return state_dir(repo) / f"{repo_slug(repo)}.json"


def journal_path(repo: Path) -> Path:
    return state_dir(repo) / f"{repo_slug(repo)}.jsonl"


def lock_path(repo: Path) -> Path:
    return state_dir(repo) / f"{repo_slug(repo)}.lock"


def load_state(repo: Path) -> dict:
    p = state_path(repo)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("version") == STATE_VERSION:
            payload.setdefault("quests", {})
            payload.setdefault("convoys", {})
            payload.setdefault("spawns", [])
            return payload
    except (OSError, ValueError):
        pass
    return {"version": STATE_VERSION, "repo": str(repo), "quests": {}, "convoys": {}, "spawns": []}


def save_state(repo: Path, state: dict) -> None:
    p = state_path(repo)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def _journal(repo: Path, event: dict) -> None:
    p = journal_path(repo)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > JOURNAL_MAX_BYTES:
            try:
                os.replace(p, p.with_suffix(".jsonl.1"))
            except OSError:
                pass
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Instance lock — one Night's Watch per repo, ever
# ---------------------------------------------------------------------------


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


def acquire_instance_lock(repo: Path) -> tuple[bool, str]:
    """Exclusive per-repo lock so a cron `--once` and a loop daemon cannot
    both act. A lock whose pid is gone is stolen (crash recovery)."""
    p = lock_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = {}
            try:
                holder = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            pid = holder.get("pid")
            if _pid_alive(pid):
                return False, f"another nightwatch is running (pid {pid}, started {holder.get('started_iso', '?')})"
            try:
                p.unlink()
            except OSError:
                return False, f"stale lock at {p} could not be removed"
            continue
        except OSError as e:
            return False, f"lock error: {e}"
        with os.fdopen(fd, "w") as f:
            json.dump({
                "pid": os.getpid(),
                "started": time.time(),
                "started_iso": _now_iso(),
                "repo": str(repo),
            }, f, indent=2)
        return True, "acquired"
    return False, "lock contention"


def release_instance_lock(repo: Path) -> None:
    p = lock_path(repo)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        if payload.get("pid") == os.getpid():
            p.unlink()
    except (OSError, ValueError):
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Repo observation
# ---------------------------------------------------------------------------


def _court(repo: Path, *argv: str, timeout: int = DEFAULT_ACTION_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "court.cli", *argv],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def snapshot(repo: Path, timeout: int = DEFAULT_STATUS_TIMEOUT) -> dict:
    """`court status --json` — the repo engine's own audit + worker verdicts."""
    res = _court(repo, "status", "--json", timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(
            f"court status --json failed (exit {res.returncode}): "
            f"{(res.stderr or res.stdout or '').strip()[:400]}"
        )
    return json.loads(res.stdout)


def live_kilo_workers(repo: Path) -> dict:
    """RAM guard: every `kilo run` whose --dir points into this repo's
    worktrees OR at the repo root (a Steward triage session runs on castle
    itself). Returns count + total RSS + per-role and per-worktree maps."""
    marker = str(repo.resolve() / ".kilo" / "worktrees")
    root_marker = str(repo.resolve())
    out = subprocess.run(
        ["ps", "-axo", "pid=,rss=,command="],
        capture_output=True, text=True, timeout=20,
    )
    workers = []
    for line in (out.stdout or "").splitlines():
        if marker not in line and f"--dir {root_marker}" not in line and f"--dir={root_marker}" not in line:
            continue
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, rss_s, cmd = parts
        if "kilo" not in cmd or " run " not in f" {cmd} ":
            continue
        try:
            pid = int(pid_s)
            rss = int(rss_s)
        except ValueError:
            continue
        role = "unknown"
        for candidate in ("master_of_coin", "gatekeeper", "scout", "serf", "artist", "steward"):
            if f"--agent {candidate}" in cmd:
                role = candidate
                break
        wdir = _cmd_dir(cmd)
        # Reject a prompt that merely mentions the root/worktrees path: the
        # worker's actual --dir must be this repo's root or a worktree under it.
        if wdir and wdir != root_marker and not wdir.startswith(marker + os.sep):
            continue
        workers.append({"pid": pid, "rss_kb": rss, "role": role, "dir": wdir})
    by_dir: dict[str, list] = {}
    for w in workers:
        if w["dir"]:
            by_dir.setdefault(w["dir"], []).append(w["role"])
    return {
        "count": len(workers),
        "rss_mb": round(sum(w["rss_kb"] for w in workers) / 1024.0, 1),
        "by_role": {r: len([w for w in workers if w["role"] == r]) for r in {w["role"] for w in workers}},
        "workers": workers,
        "dirs": by_dir,
    }


def _cmd_dir(cmd: str) -> str:
    """Extract and normalise the `--dir` value from a kilo command line."""
    tokens = cmd.split()
    for i, tok in enumerate(tokens):
        value = ""
        if tok == "--dir" and i + 1 < len(tokens):
            value = tokens[i + 1]
        elif tok.startswith("--dir="):
            value = tok.split("=", 1)[1]
        if value:
            try:
                return str(Path(value).resolve())
            except OSError:
                return value
    return ""



def _read_lease(worktree: Optional[str], role: str) -> Optional[dict]:
    if not worktree:
        return None
    try:
        p = Path(worktree) / ".kilo" / "watch" / f"{role}.lease.json"
        payload = json.loads(p.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _log_age(worktree: Optional[str], role: str) -> Optional[float]:
    if not worktree:
        return None
    try:
        return time.time() - (Path(worktree) / ".kilo" / f"{role}.log").stat().st_mtime
    except OSError:
        return None


def role_state(worktree: Optional[str], role: str) -> dict:
    """idle | live | stalled | watching | exited, using lease + log age.

    Mirrors court.lease.worker_verdict without importing the target repo's
    engine code (versions diverge between repos)."""
    lease = _read_lease(worktree, role)
    age = _log_age(worktree, role)
    if not lease:
        return {"state": "idle", "lease": None, "log_age": age}
    pending = bool(lease.get("pending"))
    pid = lease.get("pid")
    if pending:
        started = float(lease.get("started") or 0)
        live = (time.time() - started) < 90
    else:
        live = _pid_alive(pid)
    if live:
        threshold = STALL_THRESHOLDS.get(role, 20 * 60)
        stalled = age is not None and age > threshold
        return {"state": "stalled" if stalled else "live", "lease": lease, "pid": pid, "log_age": age}
    watchdog = lease.get("watchdog_pid")
    state = "watching" if _pid_alive(watchdog) else "exited"
    return {"state": state, "lease": lease, "pid": pid, "watchdog_pid": watchdog, "log_age": age}


WORKER_ROLES = ("serf", "scout", "artist", "master_of_coin", "gatekeeper")


def occupied(worktree: Optional[str]) -> Optional[dict]:
    """Which role, if any, currently owns this worktree.

    `court status --json`'s `worker` field only covers serf/scout. A live
    Court Artist or Master of Coin on the same worktree must also suppress a
    goad/coin — otherwise the supervisor stacks a second writer (the Q799
    class) and, worse, goads a quest an artist is actively polishing."""
    for role in WORKER_ROLES:
        st = role_state(worktree, role)
        if st["state"] in ("live", "stalled", "watching"):
            return {"role": role, **st}
    return None



# ---------------------------------------------------------------------------
# Planning — pure, testable
# ---------------------------------------------------------------------------


@dataclass
class Action:
    kind: str
    quest_ids: list = field(default_factory=list)
    cogship: str = ""
    reason: str = ""
    force: bool = False
    # Bookkeeping the planner wants applied ONLY if this action is actually
    # attempted (cooldowns, attempt counters, convoy stamps). The planner
    # itself never mutates state, so a dry-run or a tick cut short by the
    # hourly budget cannot silently consume a quest's next goad.
    meta: dict = field(default_factory=dict)


def _vigil_verb(spec: dict) -> str:
    try:
        return vigils._parse_action(spec.get("then") or "").get("verb") or ""
    except Exception:  # noqa: BLE001
        return ""


def _quest_map(snap: dict) -> dict:
    return {q["id"]: q for q in snap.get("quests", []) if q.get("kind") != "epic"}


def board_signature(quests: dict, notes: list) -> str:
    """A stable fingerprint of the pipeline's *blocked* state.

    Statuses and worker states alone are not enough: two consecutive ticks can
    look identical while the watch is legitimately working (live serf, long
    suite). The signature is compared together with the live-worker census, so
    it only measures "has anything about this board changed?" — the raw signal
    for staleness. Human escalations participate so a newly surfaced blocker
    always counts as a change."""
    rows = []
    for qid, q in sorted(quests.items()):
        worker = (q.get("worker") or {}).get("state") or "idle"
        rows.append(f"{qid}|{q.get('status')}|{worker}|{int(bool(q.get('pending_audience')))}")
    blockers = sorted(
        f"{n.get('quest', '')}|{n.get('key') or n.get('text', '')}"
        for n in notes if n.get("level") == "human"
    )
    digest = "\n".join(rows) + "\n--\n" + "\n".join(blockers)
    return hashlib.sha1(digest.encode("utf-8")).hexdigest()[:16]


def _within(ts: Optional[float], window: int, now: float) -> bool:
    return bool(ts) and (now - float(ts)) < window


def plan(snap: dict, state: dict, policy: Policy, workers: dict, now: Optional[float] = None,
         ready_vigils: Optional[list] = None) -> tuple[list, list]:
    """Decide this tick's actions. Returns (actions, notes).

    Pure function: no I/O beyond the already-collected inputs, so the policy
    can be unit-tested without touching a real repo."""
    now = time.time() if now is None else now
    quests = _quest_map(snap)
    # Read-only views: plan is pure, so bookkeeping lands in tick().
    qstate = state.get("quests") or {}
    convoys = state.get("convoys") or {}
    # One-shot escalations already delivered, keyed by quest -> {key: ts}. A
    # recurring condition (a stale gatekeeper stamp, a UI-review hold) is
    # escalated LOUDLY once and then merely re-reported, so the Steward's view
    # is not spammed by the same standing blocker every tick.
    notified = state.get("notified") or {}
    actions: list[Action] = []
    notes: list[dict] = []

    def note(qid: str, level: str, text: str, key: Optional[str] = None) -> None:
        entry = {"quest": qid, "level": level, "text": text}
        if key:
            entry["key"] = key
            entry["new"] = key not in (notified.get(qid) or {})
        notes.append(entry)

    busy_workers = workers.get("count", 0)
    capacity = max(0, policy.max_workers - busy_workers)
    busy_dirs = workers.get("dirs") or {}
    ready_vigils = ready_vigils or []

    # --- Vigils first: an explicit Steward hand-off beats the heuristics ---
    for spec, why in ready_vigils:
        try:
            argv, spawns = vigils.action_argv(spec.get("then") or "")
        except vigils.VigilError as e:
            note(spec.get("id", "?"), "human", f"unreadable vigil action: {e}")
            continue
        verb = _vigil_verb(spec)
        if spawns and verb in ("goad", "coin", "collect") and not policy.allows(verb):
            note(spec.get("id", "?"), "skip", f"vigil ready but {verb} is disabled")
            continue
        if spawns and verb == "dispatch" and not policy.allow_dispatch:
            note(spec.get("id", "?"), "skip", "vigil ready but dispatch is disabled")
            continue
        if spawns and capacity <= 0:
            note(spec.get("id", "?"), "skip",
                 f"vigil ready but worker ceiling reached ({busy_workers}/{policy.max_workers})")
            continue
        target = ""
        try:
            target = vigils._parse_action(spec["then"]).get("target") or ""
        except vigils.VigilError:
            pass
        actions.append(Action(
            kind="vigil",
            quest_ids=[target] if target else [],
            reason=f"vigil {spec['id']}: {why} → {spec.get('then')}",
            meta={"vigil_id": spec["id"], "then": spec.get("then"), "spawns": spawns},
        ))
        if spawns:
            capacity -= 1

    def process_in(worktree: Optional[str]) -> Optional[list]:
        """Roles of live kilo processes whose --dir is this worktree.

        Catches agents spawned by hand or by a session-scoped tool path, which
        leave no lease behind — the Q1049 case (a live Court Artist the status
        worker field cannot see)."""
        if not worktree:
            return None
        try:
            key = str(Path(worktree).resolve())
        except OSError:
            key = worktree
        return busy_dirs.get(key)

    # --- GATE: hand the convoy to a Gatekeeper -----------------------------
    by_cogship: dict[str, list[str]] = {}
    for q in quests.values():
        if q.get("status") != "GATE":
            continue
        if q.get("pending_audience"):
            note(q["id"], "skip", "GATE but pending Audience — parked")
            continue
        # A PENDING UI review is a Court Artist gate the watch cannot clear.
        # `court collect` will refuse the convoy, so planning a collect here
        # would burn the per-tick action budget on a guaranteed no-op (the
        # cogship-382 starvation: one UI-pending convoy ate the single slot
        # every tick, so nothing else was ever reached). Escalate once instead.
        ui = (q.get("ui_review") or "").strip()
        if ui.upper().startswith("PENDING"):
            note(q["id"], "human",
                 f"GATE blocked on PENDING UI review ({ui[:70]}) — needs a Court Artist "
                 f"(`/artist {q['id']}` or `/atelier`); withholding from collect",
                 key="ui_pending")
            continue
        role = role_state(q.get("worktree"), "gatekeeper")
        if role["state"] in ("live", "stalled", "watching"):
            continue
        if q.get("gatekeeper_session_id"):
            note(q["id"], "human",
                 f"GATE with recorded Gatekeeper {q['gatekeeper_session_id']} but no live worker — "
                 f"triage by hand (regate or clear the session stamp)",
                 key="stale_gatekeeper")
            continue
        cog = q.get("cogship_id") or ""
        convoy = convoys.get(cog or "new", {})
        if now < float(convoy.get("retry_at") or 0):
            continue
        if _within(convoy.get("last_ts"), policy.gate_cooldown, now):
            continue
        by_cogship.setdefault(cog or "new", []).append(q["id"])

    for cog, ids in by_cogship.items():
        if not policy.allows("collect"):
            note(",".join(ids), "skip", "GATE convoy ready but collect is disabled")
            continue
        if capacity <= 0:
            note(",".join(ids), "skip", f"GATE convoy ready but worker ceiling reached ({busy_workers}/{policy.max_workers})")
            continue
        actions.append(Action(
            kind="collect",
            quest_ids=ids,
            cogship="" if cog == "new" else cog,
            reason=f"{len(ids)} Quest(s) in GATE awaiting a Gatekeeper",
            meta={"convoy": cog, "retry_backoff": policy.retry_backoff,
                  "noop_backoff": policy.collect_noop_backoff},
        ))
        capacity -= 1

    # --- TRIBUTE_READY: send in the Master of Coin -------------------------
    for q in quests.values():
        if q.get("status") != "TRIBUTE_READY":
            continue
        if q.get("pending_audience"):
            note(q["id"], "skip", "TRIBUTE_READY but pending Audience — parked")
            continue
        if not q.get("worktree") or not Path(q["worktree"]).is_dir():
            note(q["id"], "human", "TRIBUTE_READY with no usable worktree")
            continue
        st = qstate.get(q["id"]) or {}
        if now < float(st.get("retry_at") or 0):
            continue
        coins = int(st.get("coins") or 0)
        if coins >= policy.max_coins:
            note(q["id"], "human", f"coin ceiling reached ({coins}/{policy.max_coins}) without leaving TRIBUTE_READY — needs a human")
            continue
        if _within(st.get("last_coin_ts"), policy.coin_cooldown, now):
            continue
        busy = occupied(q.get("worktree"))
        if busy:
            if busy["role"] != "master_of_coin" or busy["state"] != "live":
                note(q["id"], "wait", f"{busy['role']} is {busy['state']} in this worktree — not stacking a Master of Coin")
            continue
        procs = process_in(q.get("worktree"))
        if procs:
            note(q["id"], "wait", f"live {','.join(sorted(set(procs)))} process in this worktree — leaving it alone")
            continue
        if not policy.allows("coin"):
            note(q["id"], "skip", "TRIBUTE_READY but coin is disabled")
            continue
        if capacity <= 0:
            note(q["id"], "skip", f"TRIBUTE_READY but worker ceiling reached ({busy_workers}/{policy.max_workers})")
            continue
        actions.append(Action(kind="coin", quest_ids=[q["id"]],
                              reason="Tribute rendered; no live Master of Coin",
                              meta={"retry_backoff": policy.retry_backoff}))
        capacity -= 1

    # --- WORKING: restart dead workers -------------------------------------
    for q in quests.values():
        if q.get("status") not in ("WORKING", "QUESTING"):
            continue
        if q.get("pending_audience"):
            note(q["id"], "skip", "WORKING but pending Audience — parked")
            continue
        if not q.get("worktree") or not Path(q["worktree"]).is_dir():
            note(q["id"], "human", f"{q.get('status')} with no usable worktree")
            continue
        worker = (q.get("worker") or {}).get("state") or "idle"
        if worker == "live":
            continue
        if worker == "stalled":
            note(q["id"], "wait", "worker STALLED — the per-worker watchdog referee owns the kill/re-prompt")
            continue
        busy = occupied(q.get("worktree"))
        if busy:
            # A Court Artist / Master of Coin / Gatekeeper can hold the same
            # worktree while the serf/scout turn is over. Goading then would
            # stack a second writer onto live work.
            note(q["id"], "wait", f"{busy['role']} is {busy['state']} in this worktree — not goading beneath it")
            continue
        procs = process_in(q.get("worktree"))
        if procs:
            note(q["id"], "wait", f"live {','.join(sorted(set(procs)))} process in this worktree — not goading beneath it")
            continue
        st = qstate.get(q["id"]) or {}
        if now < float(st.get("retry_at") or 0):
            continue
        sid = (q.get("serf_session_id") or "").strip()
        ctx = handoff.session_context_tokens(sid) if (policy.cull_soft or policy.hard_red_zone) else None
        over_hard = bool(ctx and policy.hard_red_zone and ctx >= policy.hard_red_zone)
        over_soft = bool(ctx and policy.cull_soft and ctx >= policy.cull_soft)
        if worker == "watching":
            # A live per-worker watchdog owns the continuation and will
            # re-prompt the SAME session. A healthy session is left alone; a
            # session past the cull line is flagged (or, with
            # --bloat-cold-start --allow-force-goad, superseded by a forced
            # fresh goad below).
            if not (over_hard or over_soft):
                continue
            zone = "red zone" if over_hard else "past the cull line"
            limit = policy.hard_red_zone if over_hard else policy.cull_soft
            if not (policy.bloat_cold_start and policy.allow_force_goad):
                note(q["id"], "human",
                     f"watchdog is about to re-prompt a session in the {zone} (~{ctx:,} tokens >= "
                     f"{limit:,}) — a fresh goad would be cheaper "
                     f"(`--bloat-cold-start --allow-force-goad` makes this automatic)")
                continue
        goads = int(st.get("goads") or 0)
        serf_index = int(st.get("serf_index") or 0)
        per_serf = max(1, policy.goads_per_serf)
        budget = policy.goad_budget()
        if goads >= budget:
            note(q["id"], "human",
                 f"goad budget exhausted ({goads}/{budget} = {policy.serf_slots} serfs x "
                 f"{per_serf} goads) with worker {worker} — bubbling up to a Steward",
                 key="goad_budget")
            continue
        if _within(st.get("last_goad_ts"), policy.goad_cooldown, now):
            continue
        # How many goads has the CURRENT serf spent? (goads is cumulative; a
        # dispatch advances serf_index without spending a goad.)
        serf_goads = goads - serf_index * per_serf
        if serf_goads >= per_serf:
            # This serf has spent its goads: fall over to a REPLACEMENT dispatch
            # (a fresh serf inherits the committed prior art) rather than
            # goading the same dead session forever.
            if policy.allows("dispatch") and (serf_index + 1) < max(1, policy.serf_slots):
                if capacity <= 0:
                    note(q["id"], "skip",
                         f"serf #{serf_index + 1} spent {per_serf} goads but worker ceiling "
                         f"reached ({busy_workers}/{policy.max_workers})")
                    continue
                actions.append(Action(
                    kind="dispatch", quest_ids=[q["id"]],
                    reason=(f"serf #{serf_index + 1} spent {per_serf} goads with worker {worker} — "
                            f"dispatching replacement serf #{serf_index + 2}"),
                    meta={"serf_index": serf_index, "goads_before": goads,
                          "retry_backoff": policy.retry_backoff}))
                capacity -= 1
                continue
            note(q["id"], "human",
                 f"serf slots exhausted ({serf_index + 1}/{policy.serf_slots}) with worker {worker} — "
                 f"bubbling up to a Steward",
                 key="goad_budget")
            continue
        if not policy.allows("goad"):
            note(q["id"], "skip", f"worker {worker} but goad is disabled")
            continue
        if capacity <= 0:
            note(q["id"], "skip", f"worker {worker} but worker ceiling reached ({busy_workers}/{policy.max_workers})")
            continue
        if worker == "watching":
            why = (f"watchdog is re-prompting a session in the "
                   f"{'red zone' if over_hard else 'cull line'} (~{ctx:,} tokens) — "
                   f"superseding it with a fresh cold-start goad")
            mode = "cold"
        else:
            # Respect the context window: continue the SAME session when it is
            # healthy, and cold-start only when it is bloated — the red zone
            # always, the soft cull line only on a committed tree (an
            # uncommitted tree keeps its session so a fresh start cannot lose
            # in-flight edits). A deterministic fresh spawn on every goad is
            # exactly the churn this avoids.
            checkpointed = handoff.worktree_is_clean(q.get("worktree"))
            cold, cwhy = handoff.continuation_decision(
                ctx, policy.cull_soft, policy.hard_red_zone, checkpointed)
            cold = cold or not sid
            mode = "cold" if cold else "resume"
            why = (f"worker {worker} (turn ended, no live watchdog); "
                   f"{'cold-starting a fresh session' if cold else 'continuing the same session'} — {cwhy}")
        actions.append(Action(kind="goad", quest_ids=[q["id"]], reason=why,
                              force=bool(worker == "watching" and (over_hard or over_soft) and policy.bloat_cold_start),
                              meta={"goads_before": goads, "prior_tokens": ctx or 0, "mode": mode,
                                    "retry_backoff": policy.retry_backoff}))
        capacity -= 1

    # --- report-only stages ------------------------------------------------
    for q in quests.values():
        status = q.get("status")
        if status in ("PLANNED", "CHARTERED", "DISPATCHED"):
            note(q["id"], "report", f"{status} — waiting on a Steward dispatch decision")
        elif status == "READY_TO_RAZE":
            note(q["id"], "report", "READY_TO_RAZE — promotion verified; `court raze` is a human/Steward call")
        elif status == "HELD":
            note(q["id"], "report", "HELD — awaiting Audience")

    actions = actions[: policy.max_spawns_per_tick]
    return actions, notes


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _prune_spawns(state: dict, now: float) -> list:
    spawns = [float(t) for t in state.get("spawns", []) if (now - float(t)) < 3600]
    state["spawns"] = spawns
    return spawns


def execute(repo: Path, action: Action, timeout: int = DEFAULT_ACTION_TIMEOUT) -> dict:
    if action.kind == "vigil":
        then = action.meta.get("then") or ""
        if not action.meta.get("spawns", True):
            return {"ok": True, "exit": 0, "argv": [], "stdout_tail": f"note-only vigil: {then}",
                    "stderr_tail": "", "seconds": 0.0}
        argv, _spawns = vigils.action_argv(then)
    elif action.kind == "collect":
        # `court collect` takes ONE positional that may be a comma-separated
        # list (quest_ids, batchable) — passing N positionals makes argparse
        # reject the extras ("unrecognized arguments"), which silently broke
        # every multi-quest convoy the watch tried to pack.
        argv = ["collect"] + ([",".join(action.quest_ids)] if action.quest_ids else [])
    elif action.kind == "dispatch":
        # Bounded replacement dispatch: a spent serf is replaced by a fresh one
        # in the SAME worktree (`--replace`), inheriting the committed prior art.
        argv = ["dispatch", *action.quest_ids, "--replace"]
    else:
        argv = [action.kind, *action.quest_ids]
    if action.kind == "goad" and action.meta.get("mode"):
        argv += ["--mode", str(action.meta["mode"])]
    if action.kind == "collect":
        if action.cogship:
            argv += ["--cogship", action.cogship]
        argv.append("--standup")
    if action.force:
        argv.append("--force")
    started = time.time()
    try:
        res = _court(repo, *argv, timeout=COLLECT_TIMEOUT if action.kind == "collect" else timeout)
        out = (res.stdout or "").strip()
        err = (res.stderr or "").strip()
        return {
            "ok": res.returncode == 0,
            "exit": res.returncode,
            "argv": argv,
            "stdout_tail": out[-1200:],
            "stderr_tail": err[-1200:],
            "seconds": round(time.time() - started, 1),
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "exit": -1, "argv": argv,
            "stdout_tail": "", "stderr_tail": f"timed out after {COLLECT_TIMEOUT if action.kind == 'collect' else timeout}s",
            "seconds": round(time.time() - started, 1),
        }
    except Exception as e:  # noqa: BLE001 — a supervisor must never die on one action
        return {"ok": False, "exit": -1, "argv": argv, "stdout_tail": "", "stderr_tail": str(e)[:500],
                "seconds": round(time.time() - started, 1)}


def _collect_packed(result: dict) -> bool:
    """Did `court collect` actually mint a convoy?

    Exit 0 with `🚢 Stamped N Quest(s) onto <cogship>` is the success line.
    Exit 0 with only `Skipped ... (not ready to pack)` packed nothing — a
    UI-pending or otherwise blocked convoy — and must not be treated as
    progress."""
    if not result.get("ok"):
        return False
    return "Stamped" in (result.get("stdout_tail") or "")


def _apply_bookkeeping(state: dict, action: Action, result: dict, now: float, repo: Optional[Path] = None) -> None:
    """Record the consequences of an ATTEMPT (success or failure).

    Cooldowns and attempt counters move only here, never in `plan`, so a
    dry-run or a truncated tick cannot silently spend a Quest's next goad.

    A failed goad/coin still counts toward its attempt ceiling — three broken
    attempts is exactly the signal a human needs. But a FAILED attempt does not
    earn the full cooldown: an argparse slip or a momentarily busy lock would
    otherwise stall that quest or convoy for the whole gate/coin/goad window
    (the first real convoy pack lost an hour to exactly this). Failures get the
    short `retry_backoff` window instead; successes set the real cooldown and
    clear any pending retry."""
    backoff = float(action.meta.get("retry_backoff") or 0)
    ok = bool(result.get("ok"))
    qstate = state.setdefault("quests", {})

    if action.kind == "vigil":
        if repo is not None:
            vid = action.meta.get("vigil_id")
            for spec in vigils.load_all(repo, include_finished=True):
                if spec.get("id") == vid and spec.get("status") == "active":
                    vigils.mark_fired(repo, spec, result)
                    break
        return

    if action.kind == "goad":
        for qid in action.quest_ids:
            st = qstate.setdefault(qid, {})
            st["goads"] = int(action.meta.get("goads_before") or 0) + 1
            if ok:
                st["last_goad_ts"] = now
    elif action.kind == "dispatch":
        # A replacement dispatch advances to the next serf slot; it spends no
        # goad, and clears the goad cooldown so the new serf can be prompted.
        if ok:
            for qid in action.quest_ids:
                st = qstate.setdefault(qid, {})
                st["serf_index"] = int(action.meta.get("serf_index") or 0) + 1
                st["last_goad_ts"] = 0
    elif action.kind == "coin":
        for qid in action.quest_ids:
            st = qstate.setdefault(qid, {})
            st["coins"] = int(st.get("coins") or 0) + 1
            if ok:
                st["last_coin_ts"] = now
    elif action.kind == "collect":
        key = action.meta.get("convoy") or action.cogship or "new"
        convoy = state.setdefault("convoys", {}).setdefault(key, {})
        if ok and _collect_packed(result):
            convoy["last_ts"] = now
            convoy["retry_at"] = 0
        elif ok:
            # exit 0 but nothing packed (PENDING UI review, dirty tree, charter
            # violations) is NOT progress. Back the convoy off hard so a blocked
            # convoy cannot spend the per-tick action budget every tick and
            # starve every other quest (the cogship-382 starvation).
            noop = float(action.meta.get("noop_backoff") or DEFAULT_COLLECT_NOOP_BACKOFF)
            convoy["retry_at"] = now + noop
        elif backoff:
            # A hard failure (argparse slip, busy lock) retries quickly rather
            # than burning the long noop backoff.
            convoy["retry_at"] = now + backoff

    for qid in action.quest_ids:
        st = qstate.setdefault(qid, {})
        st["last_attempt_ts"] = now
        if ok:
            st["fails"] = 0
            st["last_error"] = ""
            st["last_kind"] = action.kind
            st["retry_at"] = 0
        else:
            st["fails"] = int(st.get("fails") or 0) + 1
            st["last_error"] = (result.get("stderr_tail") or result.get("stdout_tail") or "")[-400:]
            if backoff:
                st["retry_at"] = now + backoff


def _record_escalations(state: dict, notes: list, now: float) -> None:
    """Persist one-shot escalation keys (`note["key"]`) so a standing blocker is
    escalated LOUDLY once and then only re-reported. Keys absent from this tick's
    notes are forgotten, so a recurrence escalates afresh."""
    prior = state.get("notified") or {}
    active: dict[str, dict] = {}
    for n in notes:
        key = n.get("key")
        if not key:
            continue
        prev = (prior.get(n["quest"]) or {}).get(key)
        active.setdefault(n["quest"], {})[key] = prev or now
    state["notified"] = active


def tick(repo: Path, policy: Policy, state: dict, dry_run: bool = False, say=print) -> dict:
    now = time.time()
    spawns = _prune_spawns(state, now)
    workers = live_kilo_workers(repo)
    try:
        snap = snapshot(repo, timeout=policy.status_timeout)
    except Exception as e:  # noqa: BLE001
        say(f"⚠️  snapshot failed: {e}")
        _journal(repo, {"ts": _now_iso(), "event": "snapshot_failed", "error": str(e)[:500]})
        return {"actions": [], "notes": [], "workers": workers, "error": str(e)}

    try:
        ready, vigil_notes = vigils.ready_vigils(repo, _quest_map(snap), now=datetime.now(timezone.utc))
    except Exception as e:  # noqa: BLE001
        ready, vigil_notes = [], []
        say(f"⚠️  vigil evaluation failed: {e}")
    vigil_notice: list[dict] = []
    for vn in vigil_notes:
        vigil_notice.append({"quest": vn["id"], "level": "vigil", "text": vn["text"]})
    actions, notes = plan(snap, state, policy, workers, now=now, ready_vigils=ready)
    notes = vigil_notice + notes
    _record_escalations(state, notes, now)
    executed = []
    for action in actions:
        if len(spawns) >= policy.max_spawns_per_hour:
            say(f"\u23f8\ufe0f  hourly spawn budget reached ({policy.max_spawns_per_hour}/h) \u2014 skipping {action.kind} {','.join(action.quest_ids)}")
            notes.append({"quest": ",".join(action.quest_ids), "level": "skip",
                          "text": f"hourly spawn budget reached ({policy.max_spawns_per_hour}/h)"})
            break
        label = f"{action.kind} {','.join(action.quest_ids)}" + (f" [{action.cogship}]" if action.cogship else "")
        if dry_run:
            say(f"[dry-run] would run: {label} \u2014 {action.reason}")
            executed.append({"action": action.__dict__, "result": {"ok": True, "dry_run": True}})
            continue
        say(f"\u25b6 {label} \u2014 {action.reason}")
        result = execute(repo, action)
        spawns.append(now)
        state["spawns"] = spawns
        _apply_bookkeeping(state, action, result, now, repo)
        record = {"ts": _now_iso(), "event": "action", "action": action.__dict__, "result": result,
                  "workers_before": workers}
        _journal(repo, record)
        executed.append({"action": action.__dict__, "result": result})
        status = "\u2705" if result["ok"] else "\u274c"
        say(f"  {status} exit {result['exit']} in {result['seconds']}s")
        if not result["ok"]:
            tail = (result.get("stderr_tail") or result.get("stdout_tail") or "").strip()
            if tail:
                say("  " + tail.splitlines()[-1][:300])

    state["last_tick"] = _now_iso()
    state["last_tick_epoch"] = now
    state["pid"] = os.getpid()
    save_state(repo, state)
    _journal(repo, {
        "ts": state["last_tick"],
        "event": "tick",
        "workers": workers.get("count"),
        "rss_mb": workers.get("rss_mb"),
        "actions": [f"{e['action']['kind']}:{','.join(e['action']['quest_ids'])}" for e in executed],
        "ctx": {q["id"]: (handoff.session_context_tokens(q.get("serf_session_id") or "") or 0)
                for q in snap.get("quests", [])
                if q.get("kind") != "epic" and q.get("status") in ("WORKING", "QUESTING")},
        "human": [n["quest"] for n in notes if n["level"] == "human"],
        "human_new": [n["quest"] for n in notes if n["level"] == "human" and n.get("new")],
        "statuses": {q["id"]: q["status"] for q in snap.get("quests", []) if q.get("kind") != "epic"},
    })
    return {"actions": executed, "notes": notes, "workers": workers, "snapshot": snap,
            "vigils_active": len(vigils.load_all(repo))}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def read_journal(repo: Path, max_events: int = 20000) -> list:
    """Parse the journal (bounded to the most recent `max_events` lines)."""
    path = journal_path(repo)
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    out = []
    for line in raw[-max_events:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def render_digest(repo: Path, journal: Optional[list] = None) -> str:
    """A Steward-facing roll-up of where the Night's Watch is stuck.

    Reads the journal + state (never acts) and reports the actionable set:
    no-op/livelock collects, stale gatekeeper holds, exhausted goad/coin
    budgets, repeated failures, snapshot failures, and daemon restarts — so the
    Steward can present it to M'Lord and set up the manual work (Artists,
    regates, triage) the watch cannot do."""
    rows = read_journal(repo) if journal is None else journal
    state = load_state(repo)

    lines: list[str] = []
    lines.append(f"🧭 Night's Watch digest — {repo}")
    lock = {}
    try:
        lock = json.loads(lock_path(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    alive = _pid_alive(lock.get("pid"))
    lines.append(f"daemon: {'RUNNING pid ' + str(lock.get('pid')) if alive else 'not running'} · "
                 f"last tick {state.get('last_tick') or '(never)'}")

    # Policy from the most recent start event (records the live policy dict).
    policy = {}
    for r in rows:
        if r.get("event") == "start" and isinstance(r.get("policy"), dict):
            policy = r["policy"]
    per_serf = int(policy.get("goads_per_serf") or DEFAULT_GOADS_PER_SERF)
    slots = int(policy.get("serf_slots") or DEFAULT_SERF_SLOTS)
    budget = max(1, slots) * max(1, per_serf)

    actions = [r for r in rows if r.get("event") == "action"]
    starts = [r for r in rows if r.get("event") == "start"]
    snaps = [r for r in rows if r.get("event") == "snapshot_failed"]
    lines.append(f"journal: {len(rows)} events · {len(actions)} actions · {len(starts)} starts · "
                 f"{len(snaps)} snapshot failures")
    if snaps:
        lines.append(f"  ⚠️ last snapshot failure: {snaps[-1].get('ts')} — {str(snaps[-1].get('error'))[:160]}")

    # --- Livelock: collects that packed nothing ---------------------------------
    def _packed(r):
        return "Stamped" in (r["result"].get("stdout_tail") or "")
    noops: dict[str, dict] = {}
    for r in actions:
        if r["action"]["kind"] != "collect":
            continue
        key = r["action"].get("cogship") or r["action"]["meta"].get("convoy") or "new"
        rec = noops.setdefault(key, {"count": 0, "ok": 0, "quests": set(), "first": None, "last": None, "reasons": set()})
        rec["count"] += 1
        rec["quests"].update(r["action"]["quest_ids"])
        rec["first"] = rec["first"] or r["ts"]
        rec["last"] = r["ts"]
        out = (r["result"].get("stdout_tail") or "")
        if r["result"].get("ok") and not _packed(r):
            rec["ok"] += 1
            for line in out.splitlines():
                if "not ready to pack" in line or "UI Review is PENDING" in line:
                    rec["reasons"].add(line.strip()[:120])
    fragile = {k: v for k, v in noops.items() if v["ok"] >= 2}
    if fragile:
        lines.append("")
        lines.append("livelock — collects that packed nothing (candidate fix: Court Artist / unblock):")
        for key, v in sorted(fragile.items(), key=lambda kv: -kv[1]["ok"]):
            qs = ",".join(sorted(v["quests"]))
            lines.append(f"  ✗ convoy {key}: {v['ok']}/{v['count']} no-op collects, {v['first']} → {v['last']}")
            lines.append(f"     quests: {qs}")
            for reason in sorted(v["reasons"]):
                lines.append(f"     reason: {reason}")

    # --- Stale gatekeeper holds -------------------------------------------------
    holds = []
    for r in rows:
        if r.get("event") != "tick":
            continue
        for qid in r.get("human") or []:
            holds.append(qid)
    # current standing escalations from state
    notified = state.get("notified") or {}
    if notified:
        lines.append("")
        lines.append("standing escalations (escalated once; not re-spammed):")
        for qid, keys in sorted(notified.items()):
            for key in keys:
                lines.append(f"  🔔 {qid}: {key}")

    # --- Budgets / counters -----------------------------------------------------
    quests = state.get("quests") or {}
    lines.append("")
    lines.append(f"worker-recovery budget: {slots} serfs x {per_serf} goads = {budget} goads per Quest")
    spent = []
    for qid, st in sorted(quests.items()):
        g = int(st.get("goads") or 0)
        si = int(st.get("serf_index") or 0)
        if g or st.get("coins") or st.get("fails"):
            spent.append((qid, g, si, int(st.get("coins") or 0), int(st.get("fails") or 0), st.get("last_error") or ""))
    if spent:
        lines.append("  Quest                        goads/serf  coins  fails")
        for qid, g, si, coins, fails, err in spent:
            flag = " ← at budget" if g >= budget else ""
            lines.append(f"  {qid[:28]:<28} {g}/{budget} (serf {si + 1})  {coins}     {fails}{flag}")
            if fails and err:
                lines.append(f"      last error: {err.splitlines()[-1][:120] if err.strip() else ''}")

    # --- Failures ---------------------------------------------------------------
    fails = [r for r in actions if not r["result"].get("ok")]
    if fails:
        lines.append("")
        lines.append(f"failed actions ({len(fails)}):")
        for r in fails[-8:]:
            tail = (r["result"].get("stderr_tail") or r["result"].get("stdout_tail") or "").strip().splitlines()
            lines.append(f"  ✗ {r['ts']} {r['action']['kind']} {','.join(r['action']['quest_ids'])} "
                         f"— exit {r['result'].get('exit')}: {tail[-1][:120] if tail else ''}")

    if not fragile and not notified and not spent and not fails:
        lines.append("")
        lines.append("✅ no live issues detected in the journal/state window.")
    return "\n".join(lines)


def render_report(snap: dict, notes: list, workers: dict, executed: list, dry_run: bool,
                  vigils_active: int = 0) -> str:
    quests = _quest_map(snap)
    lines = []
    head = "🧭 Night's Watch" + (" (dry-run)" if dry_run else "")
    lines.append(f"{head} — {_now_iso()}")
    lines.append(
        f"workers: {workers.get('count', 0)} live · {workers.get('rss_mb', 0)} MB RSS"
        + (f" · {workers.get('by_role')}" if workers.get("by_role") else "")
    )
    if vigils_active:
        lines.append(f"vigils: {vigils_active} active (conditions re-checked every tick)")
    if executed:
        lines.append("")
        lines.append("actions this tick:")
        for e in executed:
            a = e["action"]
            r = e["result"]
            mark = "✓" if r.get("ok") else "✗"
            lines.append(f"  {mark} {a['kind']} {','.join(a['quest_ids'])} — {a['reason']}")
    watch = [q for q in quests.values() if q.get("status") in
             ("WORKING", "QUESTING", "TRIBUTE_READY", "GATE")]
    if watch:
        lines.append("")
        lines.append("pipeline:")
        for q in sorted(watch, key=lambda x: x["id"]):
            worker = (q.get("worker") or {}).get("state") or "idle"
            sid = (q.get("serf_session_id") or "").strip()
            ctx = handoff.session_context_tokens(sid) if sid else None
            ctx_bit = f" ctx={ctx // 1000}k" if ctx else ""
            lines.append(f"  {q['id']:<10} {q['status']:<15} worker={worker}{ctx_bit}")
    fired = [n for n in notes if n["level"] == "vigil"]
    if fired:
        lines.append("")
        lines.append("vigils:")
        for n in fired:
            lines.append(f"  🕯 {n['quest']}: {n['text']}")
    human = [n for n in notes if n["level"] == "human"]
    if human:
        lines.append("")
        lines.append("needs a human:")
        for n in human:
            flag = "🔔 NEW" if n.get("new") else "⚑"
            lines.append(f"  {flag} {n['quest']}: {n['text']}")
    waiting = [n for n in notes if n["level"] == "wait"]
    if waiting:
        lines.append("")
        lines.append("letting the machinery work:")
        for n in waiting:
            lines.append(f"  … {n['quest']}: {n['text']}")
    if not executed:
        held = [n for n in notes if n["level"] == "skip"]
        if held:
            lines.append("")
            lines.append("held this tick:")
            for n in held[:8]:
                lines.append(f"  ⏸ {n['quest']}: {n['text']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------


def run_loop(repo: Path, policy: Policy, interval: int, once: bool, dry_run: bool, quiet: bool = False) -> int:
    ok, why = acquire_instance_lock(repo)
    if not ok:
        print(f"ERROR: {why}", file=sys.stderr)
        return 2
    stop = {"flag": False}

    def _handle(signum, _frame):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    def say(*args, **kwargs):
        if not quiet:
            print(*args, flush=True, **kwargs)

    state = load_state(repo)
    _journal(repo, {"ts": _now_iso(), "event": "start", "pid": os.getpid(), "dry_run": dry_run,
                    "policy": policy.__dict__, "interval": interval, "once": once})
    rc = 0
    try:
        while True:
            say("")
            try:
                result = tick(repo, policy, state, dry_run=dry_run, say=say)
                snap = result.get("snapshot") or {"quests": []}
                say(render_report(snap, result.get("notes", []), result.get("workers", {}),
                                  result.get("actions", []), dry_run,
                                  result.get("vigils_active", 0)))
            except Exception as e:  # noqa: BLE001 — a supervisor must survive its own bugs
                rc = 1
                say(f"⚠️  tick failed: {e}")
                _journal(repo, {"ts": _now_iso(), "event": "tick_failed", "error": str(e)[:800]})
            if once:
                break
            deadline = time.time() + interval
            while not stop["flag"] and time.time() < deadline:
                time.sleep(min(5, max(0.2, deadline - time.time())))
            if stop["flag"]:
                break
    finally:
        _journal(repo, {"ts": _now_iso(), "event": "stop", "pid": os.getpid(), "reason": "signal" if stop["flag"] else "done"})
        release_instance_lock(repo)
    return rc
