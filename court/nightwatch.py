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
  * WORKING/TRIBUTE_READY/GATE behind   -> `rebase` (mechanical convergence onto
    `castle`                               castle before a spawn; zero agent turns)
  * GATE + no live/recorded Gatekeeper  -> `collect --standup` (a PENDING UI
                                           review is escalated, never collected)
  * GATE + UI review PENDING            -> `atelier` (opt-in `--enable ...,atelier`):
                                           merge the Quest into the ONE standing
                                           studio for review; a live artist turn
                                           defers the merge (single-writer)
  * PLANNED/CHARTERED/DISPATCHED        -> report only (dispatching new work is a
                                           Steward judgment call, not a timer —
                                           the one exception is a bounded
                                           replacement serf for a spent Quest)
  * READY_TO_RAZE                       -> `raze`: `court raze` verifies promotion,
                                           then removes the worktree, the merged
                                           branch, and its pytest databases
                                           (default-on; `--keep-worktree` on the
                                           verb leaves everything for a human)
  * open charter request                -> `charter`: one bounded Steward turn
                                           authors the charter and allocates the
                                           Quest number (requests-in, charters-out)
  * READY_TO_RAZE that raze refuses     -> `regate`: a squashed/diverged convoy is
                                           handed back to a fresh convoy; after
                                           `max_regates` failures the Quest is
                                           escalated to a Steward
  * dead convoy (stale gatehouse)       -> `teardown` the provably-dead gatehouse
                                           worktree so `collect` can re-create it
  * stale board, nobody running         -> `muster` ONE bounded Steward triage
                                           session (opt-in; see below)

Convoy sizing is policy, not an accident: unstamped GATE Quests share one
cohort and wait for `--collect-min-batch` peers (bounded by
`--collect-batch-window`) so one gatehouse worktree and one unified suite cover
several Quests; `--collect-batch-max` caps a cohort. Raze-refused
READY_TO_RAZE Quests are re-gated as one convoy per app cohort
(`--regate-batch-max`) for the same reason, and only one multi-Quest gatehouse
convoy is planned per tick (the collect guard serialises gatehouses anyway).

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
import re
import shutil
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
# A convoy whose gatehouse worktree still exists from a prior mid-integration
# death cannot be re-collected: `court collect` stamps the quests, then refuses
# to re-create the worktree and exits 1. Re-stamping every tick is pure churn,
# so hold the convoy (escalate once) until the worktree is torn down.
DEFAULT_STALE_CONVOY_BACKOFF = 60 * 60
# A stale gatehouse teardown that keeps failing is not going to fix itself on
# the next tick: after this many failed `git worktree remove` attempts the watch
# stops retrying and escalates the exact manual-removal commands instead.
DEFAULT_MAX_TEARDOWN_FAILS = 2
# A brand-new gatehouse worktree has no session for a few seconds while
# `collect` creates it and stands up the Gatekeeper. The proactive dead-gatehouse
# sweep must not race that window, so a worktree younger than this is ignored.
DEFAULT_GATEHOUSE_GRACE = 10 * 60
# A new convoy cannot be packed while another is mid-integration (`court
# collect` reports "convoy <id> is LIVE"). That is a busy signal, not a Quest
# failure: wait out the gate window instead of retrying on the short backoff.
DEFAULT_LIVE_CONVOY_BACKOFF = 60 * 60
DEFAULT_RETRY_BACKOFF = 300
# A convoy that keeps failing the same collect does not heal on a fixed
# 5-minute retry loop (the 2026-11-05 the-gatehouse-cogship-381/399 storm:
# 23 identical failures at 300s apiece, ~2h of churn). Each consecutive
# failure doubles the retry window up to `max_retry_backoff`, and at
# `max_fails` the convoy is escalated as a dead letter so a Steward looks at
# the actual blocker instead of the watch re-stamping it every tick. The
# convoy keeps retrying at the capped window, so fixing the blocker (or a
# restart) still resumes it without a manual state reset.
DEFAULT_MAX_FAILS = 3
DEFAULT_MAX_RETRY_BACKOFF = 60 * 60
# A Quest that is merely BEHIND `castle` fails its coin/goad spawn once
# (`_sync_base_before_spawn` aborts on a `git merge castle` conflict), gets a
# merge_conflict marker, and burns a Steward muster. Drift is mechanical, so a
# `rebase` action converges the worktree first and the coin lands on the next
# tick. A short cooldown keeps a persistently conflicted worktree from being
# retried every few seconds.
DEFAULT_REBASE_COOLDOWN = 300
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
# Session-store maintenance ("session razing"). The Kilo session/event store is
# the perennial disk hog (it reached 15 GB on 2026-11-08 and hit 111 GB in the
# 2026-09-28 incident). Razing ages sessions out in share-safe chunks and then
# vacuums the file. It is housekeeping, not work: the watch only runs it when
# the board is genuinely idle (no live worker, no worker-spawning action) because
# VACUUM takes an exclusive lock that would fail a worker's write.
DEFAULT_SESSION_RETENTION_DAYS = 14
DEFAULT_SESSION_PRUNE_INTERVAL = 24 * 60 * 60
DEFAULT_SESSION_KEEP_EVENTS = 1_200_000
# A `raze` whose branch tip is not an ancestor of castle (squash/merge history)
# cannot be razed until the Quest is re-gated. That is mechanical: `court collect
# --regate` hands it back to a fresh convoy. Two consecutive failures means the
# regate itself is broken, so the Quest bubbles up to a Steward instead of
# looping. Test-database reaping is housekeeping like session pruning: interval
# gated, idle-board only.
DEFAULT_MAX_REGATES = 2
DEFAULT_REGATE_COOLDOWN = 900
# Convoy sizing. A GATE Quest with no convoy yet waits for a cohort of
# `collect_min_batch` peers (or `collect_batch_window` seconds, whichever comes
# first) so N Quests share one gatehouse worktree and one unified suite run
# instead of N singleton convoys; `collect_batch_max` caps a cohort.
# `collect_min_batch=1` restores the old collect-on-first-sighting behaviour.
# Regates are batched by app and capped at `regate_batch_max` Quests per fresh
# convoy so conflict resolution stays tractable.
DEFAULT_COLLECT_MIN_BATCH = 2
DEFAULT_COLLECT_BATCH_WINDOW = 30 * 60
DEFAULT_COLLECT_BATCH_MAX = 6
DEFAULT_REGATE_BATCH_MAX = 4
# A GATE Quest can keep a recorded Gatekeeper session from a shipment whose
# worker is gone. When the convoy was NOT promoted that stamp is a
# duplicate-Gatekeeper guard — but a dead one. Park it for a human only until
# this expiry, then the watch clears the stale stamp itself and lets the Quest
# re-pack. Without this the hold is permanent: one misclassified Quest starves
# the cohort (and the 30-minute window fires a singleton) forever.
DEFAULT_STALE_GATEKEEPER_EXPIRY = 30 * 60
# Standing-atelier merge-in. UI-review-pending GATE Quests cannot be packed
# (`court collect` refuses them), so without a driver they pile up until a human
# merges them into the ONE long-lived studio by hand. The watch feeds the studio
# mechanically: `court atelier --standing <ids>` is a local git merge + server
# ensure, NEVER a reasoning worker, so it is a LOCAL action that consumes no
# worker slot. Single-writer: a live artist turn in the atelier worktree refuses
# the merge (the guard lives inside `court atelier`), so the lane stands down and
# retries when the artist is idle. Opt-in (`--enable ...,atelier`).
DEFAULT_ATELIER_COOLDOWN = 30 * 60
DEFAULT_ATELIER_BATCH_MAX = 6
DEFAULT_PG_PRUNE_INTERVAL = 6 * 60 * 60
# A charter request is intent from the Pleasance lane; one bounded Steward turn
# authors the charter (and allocates the Quest number, which the requester
# cannot do without seeing the board).
DEFAULT_MAX_CHARTER_FAILS = 2
DEFAULT_CHARTER_COOLDOWN = 30 * 60
PRUNE_TIMEOUT = 900
SESSION_PRUNE_TIMEOUT = 2400
DEFAULT_STATUS_TIMEOUT = 240
DEFAULT_ACTION_TIMEOUT = 600
COLLECT_TIMEOUT = 900
JOURNAL_MAX_BYTES = 2 * 1024 * 1024

# A pickup rejection that repeats identically is journaled at the start of the
# streak and at these counts only — never on every tick.
PICKUP_STREAK_MILESTONES = (3, 10, 25, 100)

# Worker log silence thresholds, mirroring court.lease.STALL_THRESHOLDS. Kept
# local so the supervisor stays runnable against a repo whose engine version
# differs from this one (pb-app and kilo-castle engines are separate copies).
STALL_THRESHOLDS = {
    "serf": 20 * 60,
    "scout": 30 * 60,
    "gatekeeper": 45 * 60,
    "master_of_coin": 30 * 60,
}

ENABLED_ACTIONS = ("goad", "coin", "collect", "dispatch", "steward", "charter",
                   "raze", "regate", "teardown", "rebase", "prune", "pgprune",
                   "atelier")

# A board is only "live" while at least one Quest is in a pre-terminal stage.
# Statuses the watch can move mechanically are excluded from the `steward`
# trigger's blocker test; these are the stages where only a Steward decision
# (dispatch/regate/raze/audience) can move the board.
ACTIVE_STAGES = (
    "PLANNED", "CHARTERED", "DISPATCHED", "WORKING", "QUESTING",
    "TRIBUTE_READY", "GATE", "READY_TO_RAZE",
)
STEWARD_ONLY_STAGES = ("PLANNED", "CHARTERED", "DISPATCHED", "READY_TO_RAZE")

# A Quest in one of these states can never be "blocking a spawn": it is shipped
# (DONE) or already promoted onto the trunk awaiting teardown (READY_TO_RAZE). A
# merge_conflict marker for such a Quest is stale -- the marker only clears on a
# later successful rebase/coin, so without pruning it persists forever and buries
# the live blockers (the 2026-11-14 report: 31 conflicts listed, ~23 stale).
CONFLICT_TERMINAL_STATUSES = ("DONE", "READY_TO_RAZE", "LANDED", "LAUNCHED")


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
    stale_convoy_backoff: int = DEFAULT_STALE_CONVOY_BACKOFF
    max_teardown_fails: int = DEFAULT_MAX_TEARDOWN_FAILS
    gatehouse_grace: int = DEFAULT_GATEHOUSE_GRACE
    retry_backoff: int = DEFAULT_RETRY_BACKOFF
    # Consecutive-failure handling for retried actions: exponential backoff
    # capped at `max_retry_backoff`, escalated as a dead letter at `max_fails`.
    max_fails: int = DEFAULT_MAX_FAILS
    max_retry_backoff: int = DEFAULT_MAX_RETRY_BACKOFF
    rebase_cooldown: int = DEFAULT_REBASE_COOLDOWN
    # Steward muster (last resort). See DEFAULT_STEWARD_* above.
    steward_stale_after: int = DEFAULT_STEWARD_STALE_AFTER
    steward_cooldown: int = DEFAULT_STEWARD_COOLDOWN
    max_stewards_per_day: int = DEFAULT_MAX_STEWARDS_PER_DAY
    # Raze/teardown help (destructive): a `raze` stays opt-in because it deletes
    # a branch ref; a `teardown` is default-on because a dead gatehouse worktree
    # is a very common failure mode that otherwise blocks every collect for
    # that convoy forever. Teardown is still tightly gated: it only ever fires
    # for a stale convoy whose gatehouse worktree is proven dead (no lease, no
    # process, no fresh session) and whose path matches `.kilo/worktrees/
    # the-gatehouse-*`.
    raze_cooldown: int = 1800
    # Raze support: a raze that `court raze` refuses (branch tip not an ancestor
    # of castle) is handed back to a fresh convoy automatically instead of
    # waiting for a human; `max_regates` bounds the retries before a Steward is
    # asked. Test-database reaping is interval + idle-board gated.
    max_regates: int = DEFAULT_MAX_REGATES
    regate_cooldown: int = DEFAULT_REGATE_COOLDOWN
    collect_min_batch: int = DEFAULT_COLLECT_MIN_BATCH
    collect_batch_window: int = DEFAULT_COLLECT_BATCH_WINDOW
    collect_batch_max: int = DEFAULT_COLLECT_BATCH_MAX
    # How long a GATE Quest may hold a dead Gatekeeper session stamp (convoy not
    # promoted, no live worker) before the watch clears it and re-packs.
    stale_gatekeeper_expiry: int = DEFAULT_STALE_GATEKEEPER_EXPIRY
    regate_batch_max: int = DEFAULT_REGATE_BATCH_MAX
    atelier_cooldown: int = DEFAULT_ATELIER_COOLDOWN
    atelier_batch_max: int = DEFAULT_ATELIER_BATCH_MAX
    pg_prune_interval: int = DEFAULT_PG_PRUNE_INTERVAL
    max_charter_fails: int = DEFAULT_MAX_CHARTER_FAILS
    charter_cooldown: int = DEFAULT_CHARTER_COOLDOWN
    cull_soft: int = DEFAULT_CULL_SOFT
    hard_red_zone: int = DEFAULT_HARD_RED_ZONE
    # Session-store razing (opt in with `--enable ...,prune`). Runs at most once
    # per interval, only on a fully idle board, and vacuums after pruning.
    session_retention_days: int = DEFAULT_SESSION_RETENTION_DAYS
    session_prune_interval: int = DEFAULT_SESSION_PRUNE_INTERVAL
    session_keep_events: int = DEFAULT_SESSION_KEEP_EVENTS
    session_prune_vacuum: bool = True
    bloat_cold_start: bool = False
    # A dispatch is only ever reachable through an explicitly registered
    # vigil (which carries its own reason, expiry, and fire cap) — the watch
    # never invents new work on its own.
    allow_dispatch: bool = True
    # Default to the safe mechanical set plus dead-gatehouse teardown (a common
    # blocking failure the watch must be able to clear unattended). `rebase` is
    # also default-on: converging a drifted worktree onto castle is a local,
    # zero-token git operation that prevents a deterministic coin/goad failure
    # (and the wasted Steward muster that follows). `steward` and `raze` remain
    # deliberate opt-ins: steward spends a reasoning worker; raze deletes a
    # branch ref. ENABLED_ACTIONS is the full `--enable` vocabulary.
    enabled: tuple = ("goad", "coin", "collect", "dispatch", "teardown", "rebase",
                      "raze", "regate", "charter", "pgprune")
    allow_force_goad: bool = False
    status_timeout: int = DEFAULT_STATUS_TIMEOUT
    # Pleasance lane: each tick may fetch and apply remote paperwork (charters,
    # vigils) before planning. Only the castle checkout may do this; the tick
    # refuses on any other branch so a watch started on `main` cannot commit
    # paperwork onto the wrong trunk. The pulse is written locally every tick
    # and published to the disposable telemetry branch by default so an away
    # steward can see liveness without extra flags; `--no-publish-pulse` keeps
    # it local-only.
    pickup: bool = True
    # A paid lane that has been blocked for a while by the SAME local garbage
    # (a Scout's untracked probe SQL, a regenerated audit report) does not need
    # a Steward: it needs one mechanical move. After `pickup_remedy_after`
    # identical rejections the watch clears a dirty-tree block itself — moving
    # scratch into its quarantine dir (never deleting) and restoring
    # regenerable reports — then retries the pickup once. Rejections whose
    # paths are real local edits stay escalated. Rate-limited per repo so a
    # fresh blocker is not remediated on every tick.
    pickup_remedy: bool = True
    pickup_remedy_after: int = 2
    pickup_remedy_cooldown: int = 900
    publish_pulse: bool = True
    pulse_branch: str = "status/pleasance"

    def allows(self, kind: str) -> bool:
        return kind in self.enabled

    def describe(self) -> str:
        """One-line effective policy, for the journal banner and `--status`.

        Two schedulers per repo is a normal shape, and the tuned one can lose
        the lock race to a looser one. Naming the live policy where it is read
        makes "which scheduler is actually acting?" a status check instead of a
        `ps` archaeology dig.
        """
        return (
            f"enable={','.join(self.enabled)} · workers<={self.max_workers} · "
            f"spawns={self.max_spawns_per_tick}/tick,{self.max_spawns_per_hour}/h · "
            f"goad={self.goad_cooldown}s coin={self.coin_cooldown}s gate={self.gate_cooldown}s "
            f"raze={self.raze_cooldown}s rebase={self.rebase_cooldown}s"
        )

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
            payload.setdefault("drain_spawns", [])
            return payload
    except (OSError, ValueError):
        pass
    return {"version": STATE_VERSION, "repo": str(repo), "quests": {}, "convoys": {},
            "spawns": [], "drain_spawns": []}


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


def _iso_from_epoch(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return str(ts)


# ---------------------------------------------------------------------------
# Repo observation
# ---------------------------------------------------------------------------


def _court(repo: Path, *argv: str, timeout: int = DEFAULT_ACTION_TIMEOUT,
           env: Optional[dict] = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ}
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "court.cli", *argv],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=full_env,
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


# Action kinds that run LOCAL git / bookkeeping only — they start no worker, so
# they must not be bounded by the per-tick WORKER cap nor charged against the
# hourly SPAWN budget. `rebase` and `advance` arrive as non-spawning vigils;
# `raze`, `teardown`, `prune`, `pgprune` are mechanical housekeeping; `atelier`
# is a local git merge + server ensure into the standing studio.
LOCAL_ACTION_KINDS = frozenset({"rebase", "advance", "note", "raze", "teardown",
                                "prune", "pgprune", "atelier", "cleargate"})


def _action_spawns(action: "Action") -> bool:
    """Whether an action starts a worker (and so consumes capacity and the
    hourly spawn budget).

    A vigil's own `spawns` flag is authoritative: `dispatch`/`goad`/`coin`/
    `collect`/`muster` spawn; `rebase`/`advance`/`note` do not."""
    if action.kind == "vigil":
        return bool(action.meta.get("spawns", True))
    return action.kind not in LOCAL_ACTION_KINDS


def _journal_action_spawns(action: dict) -> bool:
    """Whether a journalled action (its serialized `__dict__`) started a worker.

    Mirrors `_action_spawns` but reads the JSON shape, so the digest can count
    REAL spawns instead of the once-per-boot `start` rows (which made the old
    "0 starts" line read as "the watch spawned nothing")."""
    kind = str(action.get("kind") or "")
    if kind == "vigil":
        return bool((action.get("meta") or {}).get("spawns", True))
    return kind not in LOCAL_ACTION_KINDS


# The pipeline's in-flight exit path. A Gatekeeper convoy (collect) and a Master
# of Coin (coin) drain work that is already at the gate; everything else starts
# new work or repairs a lane.
DRAIN_KINDS = frozenset({"collect", "coin"})

# Bounded share of the rolling hourly spawn budget reserved for the drain lanes
# whenever drain work is pending (a GATE Quest awaiting a Gatekeeper or an
# unparked TRIBUTE_READY Quest awaiting a Master of Coin).
#
# The hourly budget is a rate, but it is spent first-come-first-served: the
# dispatch/charter/goal lanes are planned ahead of the drain heuristics, so a
# busy first twenty minutes of the hour could spend every slot and every queued
# collect/coin was then skipped until the hour rolled (2026-11-15 journal:
# "hourly spawn budget reached (8/h) — skipping coin Q1495-...", repeated for
# 40+ minutes with 35 TRIBUTE_READY and 3 GATE quests waiting). Reserving the
# last quarter of the hour for the drain lanes guarantees a Gatekeeper/Master of
# Coin gets the tail of every hour; the reservation is never more than half the
# cap, so dispatch keeps moving.
DRAIN_HOURLY_RESERVE_SHARE = 0.25

# One-free-worker-slot drain guarantee. The `later_hold` half-reserve above
# needs capacity >= 2, so with exactly one slot free a queue-driven dispatch
# vigil took it on EVERY tick and collect/coin were never even planned (the
# `TRIBUTE_READY but worker ceiling reached` note in the journal). The slot is
# therefore held back after at most ONE_SLOT_DRAIN_MAX_DEFERRALS consecutive
# deferrals, so a drain action is planned within two ticks of drain work
# becoming pending.
ONE_SLOT_DRAIN_MAX_DEFERRALS = 1
SPAWN_WINDOW_SECONDS = 3600


def _count_within(stamps, now: float, window: float = SPAWN_WINDOW_SECONDS) -> int:
    """Count timestamps inside the rolling `window` ending at `now`.

    Tolerates a corrupt/non-numeric stamp in persisted state: a bad entry is
    skipped rather than aborting the whole tick."""
    n = 0
    for t in stamps or ():
        try:
            if now - float(t) < window:
                n += 1
        except (TypeError, ValueError):
            continue
    return n


def _spawn_class(action: "Action") -> str:
    """The class a spawning action belongs to, for the per-class budget cap: a
    vigil is classified by its verb (dispatch/coin/collect/...)."""
    if action.kind == "vigil":
        return _vigil_verb({"then": action.meta.get("then") or ""}) or "vigil"
    return action.kind


def _spawn_rank(action: "Action") -> int:
    """Slot-allocation rank for a spawning action: 0 for the in-flight drain path
    (collect/coin), 1 for everything else. Only this pair is re-ranked; every
    other action keeps its existing plan order, which is stable within a tier."""
    return 0 if _spawn_class(action) in DRAIN_KINDS else 1


def _drain_pending(quests: dict) -> bool:
    """Whether in-flight work wants a worker slot this tick: a GATE Quest waiting
    for a Gatekeeper, or an unparked TRIBUTE_READY Quest waiting for a Master of
    Coin. Deliberately a cheap over-approximation — the reservation it drives is
    a single slot, and each drain heuristic re-checks its own guards."""
    for q in quests.values():
        if (q.get("status") or "").upper() in ("GATE", "TRIBUTE_READY") and not q.get("pending_audience"):
            return True
    return False


def _gate_cohort_short(quests: dict, policy: "Policy") -> bool:
    """Whether the gate holds fewer packable Quests than a full convoy.

    A convoy packs up to ``collect_batch_max`` Quests, but `court collect` will
    not pack a full ship until the gate holds that many — below it, the cohort
    waits out ``collect_batch_window`` and packs short. So while the gate is
    short, the coin lane (TRIBUTE_READY -> GATE) is the binding constraint on
    convoy fullness and must outrank the new-work/repair lanes (goad/dispatch).
    A cap of 1 means convoys are singletons — nothing to fill — so the gate is
    never treated as short. A parked (pending-Audience) Quest is M'Lord's, not
    packable tribute, and does not count toward a full ship."""
    cap = int(policy.collect_batch_max or 0)
    if cap <= 1:
        return False
    count = 0
    for q in quests.values():
        if (q.get("status") or "").upper() != "GATE" or q.get("pending_audience"):
            continue
        count += 1
        if count >= cap:
            return False
    return True


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


def _collect_cohort_ready(ids: list, qstate: dict, policy: "Policy", now: float) -> bool:
    """Whether an unstamped GATE cohort should pack now.

    The dominant cost of a convoy is the unified suite, so a singleton waits
    for peers: ready once the cohort reaches `collect_min_batch`, or once its
    oldest Quest has been in GATE for `collect_batch_window`.
    `collect_min_batch <= 1` (or a zero window) restores immediate collection.
    """
    min_batch = max(1, int(policy.collect_min_batch or 1))
    if len(ids) >= min_batch:
        return True
    window = max(0, int(policy.collect_batch_window or 0))
    if window <= 0:
        return True
    for qid in ids:
        since = (qstate.get(qid) or {}).get("gate_since_ts")
        if since and (now - float(since)) >= window:
            return True
    return False


def _norm_cogship(value: str) -> str:
    """Canonical ``cogship-NNN`` form (tolerates ``Cogship-606``, a branch name)."""
    try:
        from . import convoy_guard
        return convoy_guard.normalize_cogship_id(value)
    except Exception:  # noqa: BLE001 — normalize must never kill planning
        return (value or "").strip()


def _promoted_cogships(repo: Optional[Path], candidates=None) -> set:
    """Cogship ids whose convoy has already been promoted onto the trunk.

    The convoy re-ship decision must key on the CONVOY's state, not the per-Quest
    ``cogship_promoted_commit`` field: that field is written inconsistently and is
    often left empty even after the cargo shipped (the 2026-11-14 Q1460 case —
    cogship-606 reads ``promoted_at`` while Q1460's field was blank), so a
    genuinely re-shippable Quest was parked as a ``stale_gatekeeper`` hold and the
    cohort collapsed to a singleton.

    Read-only and cheap. Sources: the convoy lifecycle record's ``promoted_at`` /
    ``promoted_commit`` (``.court/convoys/<cogship>.json``), plus — for the
    candidate cogships actually in play — the convoy branch already being an
    ancestor of the trunk.
    """
    if repo is None:
        return set()
    promoted: set = set()
    convoy_dir = Path(repo) / ".court" / "convoys"
    try:
        if convoy_dir.is_dir():
            for path in convoy_dir.glob("cogship-*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                cid = _norm_cogship(payload.get("cogship_id") or path.stem)
                if cid and (payload.get("promoted_at") or payload.get("promoted_commit")):
                    promoted.add(cid)
    except Exception:  # noqa: BLE001 — observation must never kill a tick
        pass
    # A convoy whose branch is already merged into the trunk is promoted even if
    # its record lacks the stamp. Bounded to the candidates plan actually cares
    # about, so this never walks every branch.
    for cog in {_norm_cogship(c) for c in (candidates or []) if c} - promoted:
        if not cog:
            continue
        branch = f"the-gatehouse/{cog}"
        try:
            res = subprocess.run(
                ["git", "-C", str(repo), "merge-base", "--is-ancestor", branch, "castle"],
                capture_output=True, timeout=30,
            )
        except Exception:  # noqa: BLE001
            continue
        if res.returncode == 0:
            promoted.add(cog)
    return promoted


def _failure_backoff(base: float, fails: int, cap: Optional[float] = None) -> float:
    """Retry window for an action that has now failed `fails` times in a row.

    First failure waits `base`, then doubles per consecutive failure (300s,
    600s, 1200s ...), capped at `cap` when given. A single transient failure
    keeps the short retry; a stuck blocker converges on the cap instead of
    hammering the pipeline every 5 minutes forever."""
    window = max(0.0, float(base)) * (2 ** max(0, int(fails) - 1))
    return min(window, float(cap)) if cap else window


def _fmt_window(seconds: float) -> str:
    """Human-readable retry window for escalation text (e.g. `1h`, `20m`)."""
    secs = max(0, int(seconds))
    if secs >= 3600 and secs % 3600 == 0:
        return f"{secs // 3600}h"
    if secs >= 60 and secs % 60 == 0:
        return f"{secs // 60}m"
    return f"{secs}s"


def _charter_ready(repo: Path, qmap: dict, now: Optional[datetime] = None) -> list:
    """Ids of CHARTERED quests (no worktree) whose castle charter's
    ``## Prerequisites`` predicates are ALL satisfied — the pleasance
    charter-driven dispatch lane. Empty prerequisites = satisfied. An
    unreadable/unparseable charter is conservatively NOT ready (it would have
    been rejected by the pleasance pickup anyway)."""
    from .models import Quest
    from . import store

    now = now or vigils.now_utc()
    ready: list = []
    for q in qmap.values():
        if (q.get("status") or "").upper() != "CHARTERED":
            continue
        if q.get("worktree"):
            continue
        p = store.find_path(q["id"], court_root=Path(repo) / ".court")
        if p is None:
            continue
        try:
            quest = Quest.from_markdown(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        satisfied = True
        for cond in quest.prerequisites():
            ok, _why = vigils.check_condition(cond, repo, qmap, now=now)
            if not ok:
                satisfied = False
                break
        if satisfied:
            ready.append(q["id"])
    return ready


def plan(snap: dict, state: dict, policy: Policy, workers: dict, now: Optional[float] = None,
         ready_vigils: Optional[list] = None, charter_ready: Optional[list] = None,
         raze_regate: Optional[list] = None,
         charter_requests: Optional[list] = None,
         ui_mandate_ids: Optional[list] = None,
         atelier_ready: Optional[list] = None, atelier_blocked: bool = False,
         ui_pending_ids: Optional[list] = None,
         promoted_cogships: Optional[list] = None) -> tuple[list, list]:
    """Decide this tick's actions. Returns (actions, notes).

    Pure function: no I/O beyond the already-collected inputs, so the policy
    can be unit-tested without touching a real repo."""
    now = time.time() if now is None else now
    quests = _quest_map(snap)
    ui_mandate_ids = set(ui_mandate_ids or [])
    promoted_cogships = {_norm_cogship(c) for c in (promoted_cogships or []) if c}
    atelier_ready = list(atelier_ready or [])
    ui_pending_set = set(ui_pending_ids or [])
    # Whether the gate is short of a full convoy this tick. When it is, the coin
    # lane outranks goad/dispatch so each gatehouse can pack full (see the lane
    # ordering at the per-tick budget below).
    gate_short = _gate_cohort_short(quests, policy)
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

    # Fair split of the worker-slot pool. The queue-driven blocks (charters,
    # dispatch vigils) are planned first, so without a reserve they consume the
    # whole pool and the later lanes (collect/coin, recovery, goad) are never
    # even planned — budget fairness could not help a lane with no actions. Hold
    # the later lanes' half back so every lane with work competes for a slot; the
    # per-tick budget above then takes turns between them. It is restored before
    # the GATE/drain heuristics below.
    #
    # With exactly ONE free slot there is no half to hold, so the same rule left
    # the single slot to the queue-driven lanes forever. Count the consecutive
    # deferrals in `state["drain_slot_deferrals"]` and hold the one slot once
    # ONE_SLOT_DRAIN_MAX_DEFERRALS ticks have gone by without a drain action:
    # a drain action is then planned within two ticks of drain work appearing.
    capacity_at_start = capacity
    drain_pending = _drain_pending(quests)
    later_hold = 0
    one_slot_drain_hold = False
    if capacity >= 2 and drain_pending:
        later_hold = capacity // 2
    elif capacity == 1 and drain_pending:
        if int(state.get("drain_slot_deferrals") or 0) >= ONE_SLOT_DRAIN_MAX_DEFERRALS:
            later_hold = 1
            one_slot_drain_hold = True
    capacity -= later_hold
    # Hourly spawn-budget accounting (drain fairness). `spawns` is pruned to the
    # rolling hour at the start of the tick; `drain_spawns` records the subset
    # that belonged to the drain lanes, so the reservation can be settled.
    hourly_cap = int(policy.max_spawns_per_hour or 0)
    hourly_remaining = None  # None = no hourly cap configured
    drain_reserve_left = 0
    if hourly_cap > 0:
        hourly_remembered = _count_within(state.get("spawns"), now)
        hourly_remaining = max(0, hourly_cap - hourly_remembered)
        if drain_pending:
            drain_reserve_total = min(
                max(1, int(round(hourly_cap * DRAIN_HOURLY_RESERVE_SHARE))),
                max(1, hourly_cap // 2),
            )
            drain_reserve_left = max(0, drain_reserve_total
                                     - _count_within(state.get("drain_spawns"), now))

    # --- charter requests (requests-in, charters-out) -----------------------
    # Planned FIRST: a request is the pipeline's own input, so it must not
    # queue behind cleanup (rebases, collects) and lose the per-tick budget.
    # A request packet is intent, not a charter: the requester (usually the
    # away-mode voice agent) cannot see the board, so it must not allocate a
    # Quest number. One bounded Steward turn authors the charter.
    req_state = state.get("requests") or {}
    for packet in (charter_requests or []):
        rid = packet.get("id") or ""
        if not rid:
            continue
        if not policy.allows("charter"):
            note(rid, "report",
                 f"charter request {rid} — steward chartering disabled (`--enable ...,charter`)")
            continue
        st = req_state.get(rid) or {}
        fails = int(st.get("fails") or 0)
        if fails >= policy.max_charter_fails:
            note(rid, "human",
                 f"charter request {rid} ({packet.get('title') or ''}) failed {fails} time(s) — "
                 f"it needs a human to clarify the ask or charter it directly",
                 key="charter_exhausted")
            continue
        if _within(st.get("last_attempt_ts"), policy.charter_cooldown, now):
            continue
        if capacity <= 0:
            note(rid, "skip", f"charter request {rid} ready but worker ceiling reached")
            continue
        actions.append(Action(
            kind="charter", quest_ids=[rid],
            reason=f"charter request {rid} — {packet.get('title') or 'remote request'}",
            meta={"retry_backoff": policy.retry_backoff}))
        capacity -= 1

    busy_dirs = workers.get("dirs") or {}
    ready_vigils = ready_vigils or []

    # --- Charter lane: CHARTERED quest whose prerequisites are satisfied -----
    # The pleasance pickup lane: a charter with `status: CHARTERED` whose
    # `## Prerequisites` predicates are all satisfied is dispatched by the
    # watch — the charter itself is the authority, no synthesized vigil file.
    # The satisfaction is computed in tick() (repo I/O); plan only consumes
    # the id list, staying pure. Fresh dispatch = a NEW worktree (--standup).
    for qid in charter_ready or []:
        q = quests.get(qid)
        if not q or (q.get("status") or "").upper() != "CHARTERED":
            continue
        if q.get("worktree"):
            note(qid, "skip", "charter CHARTERED and prereqs satisfied but a worktree already exists")
            continue
        st = qstate.get(qid) or {}
        if now < float(st.get("retry_at") or 0):
            continue
        if not policy.allow_dispatch:
            note(qid, "skip", "charter CHARTERED ready but dispatch is disabled")
            continue
        if capacity <= 0:
            note(qid, "skip",
                 f"charter CHARTERED ready but worker ceiling reached ({busy_workers}/{policy.max_workers})")
            continue
        actions.append(Action(
            kind="dispatch", quest_ids=[qid],
            reason="charter CHARTERED with prerequisites satisfied — pickup lane",
            meta={"fresh": True, "serf_index": 0, "retry_backoff": policy.retry_backoff},
        ))
        capacity -= 1

    # --- Vigils first: an explicit Steward hand-off beats the heuristics ---
    for spec, why in ready_vigils:
        try:
            argv, spawns = vigils.action_argv(spec.get("then") or "", spec)
        except vigils.VigilError as e:
            note(spec.get("id", "?"), "human", f"unreadable vigil action: {e}")
            continue
        verb = _vigil_verb(spec)
        if spawns and verb in ("goad", "coin", "collect") and not policy.allows(verb):
            note(spec.get("id", "?"), "skip", f"vigil ready but {verb} is disabled")
            continue
        if verb == "rebase" and not policy.allows("rebase"):
            # Local git only, but it still converges a worktree a Steward may
            # not want touched — same `--enable ...,rebase` lever as the
            # watch's own drift pass.
            note(spec.get("id", "?"), "skip", "vigil ready but rebase is disabled")
            continue
        if spawns and verb == "dispatch" and not policy.allow_dispatch:
            note(spec.get("id", "?"), "skip", "vigil ready but dispatch is disabled")
            continue
        if spawns and verb == "muster" and not policy.allows("steward"):
            # A muster spends a reasoning worker, so it stays behind the same
            # opt-in (`--enable steward`) as the watch's own steward action.
            note(spec.get("id", "?"), "skip",
                 "vigil ready but steward (muster) is disabled")
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
            meta={"vigil_id": spec["id"], "then": spec.get("then"), "spawns": spawns,
                  "brief": spec.get("brief") or ""},
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

    # --- Drift: converge a behind worktree before spawning a worker ---------
    # A Quest that is merely behind `castle` is not broken, but its coin/goad
    # spawn aborts deterministically on `git merge castle` and leaves a
    # merge_conflict marker that escalates to a Steward. Drift is mechanical,
    # so run the engine's own `court rebase` first (local git only, no model
    # turn) and let the next tick coin the now-clean branch. The quest is
    # skipped for coin/goad in THIS tick so a stale tree is never audited.
    rebase_ids: set[str] = set()
    if policy.allows("rebase"):
        for q in quests.values():
            if q.get("status") not in ("WORKING", "TRIBUTE_READY", "GATE"):
                continue
            if q.get("pending_audience"):
                continue
            if not q.get("worktree") or not Path(q["worktree"]).is_dir():
                continue
            st = qstate.get(q["id"]) or {}
            if isinstance(st.get("merge_conflict"), dict):
                continue
            gs = q.get("git_status") or {}
            try:
                behind = int(gs.get("behind") or 0)
            except (TypeError, ValueError):
                behind = 0
            if behind <= 0:
                continue
            # Drift that carries only the Court's own bookkeeping (board
            # snapshots, ledger syncs) is not real drift: the trunk gains one
            # of those every tick, so rebasing on it re-merges the same branch
            # forever and burns the spawn budget. The worker's own
            # `git merge castle` picks bookkeeping up when it next runs.
            if gs.get("behind_code") == 0:
                note(q["id"], "skip",
                     f"{behind} commit(s) behind castle but bookkeeping-only "
                     f"(.court/) — no rebase needed")
                continue
            if gs.get("dirty") and not gs.get("dirty_disposable_only"):
                note(q["id"], "wait",
                     f"{behind} commit(s) behind castle but the worktree is DIRTY — "
                     f"a rebase waits for a commit")
                continue
            if occupied(q.get("worktree")) or process_in(q.get("worktree")):
                continue
            if _within(st.get("last_rebase_ts"), policy.rebase_cooldown, now):
                continue
            if now < float(st.get("retry_at") or 0):
                continue
            # A rebase spawns no worker — it is a local `git merge castle`
            # inside an already-idle worktree. Charging it against the worker
            # ceiling let a drift wave (four drifted Quest branches) starve the
            # spawn actions that tick, including a charter request that is the
            # pipeline's own input. The per-tick action cap still bounds it.
            actions.append(Action(
                kind="rebase", quest_ids=[q["id"]],
                reason=f"{behind} commit(s) behind castle — mechanical rebase",
                meta={"retry_backoff": policy.retry_backoff}))
            rebase_ids.add(q["id"])

    # Restore the later lanes' share held back above: the queue-driven blocks
    # (charters, dispatch vigils) have taken theirs, so collect/coin and the rest
    # compete for the reserved slots on equal terms.
    capacity += later_hold

    # --- GATE: hand the convoy to a Gatekeeper -----------------------------
    by_cogship: dict[str, list[str]] = {}
    # Only one multi-Quest convoy may be minted per tick: a pack > 1 creates a
    # gatehouse worktree, and `court collect` refuses a second collect while the
    # first gatehouse is live. Size-1 convoys run in the Quest worktree and are
    # exempt (they do not consume this slot).
    gatehouse_slot = 1
    teardown_keys: set[str] = set()
    for q in quests.values():
        if q.get("status") != "GATE":
            continue
        if q.get("pending_audience"):
            note(q["id"], "skip", "GATE but pending Audience — parked")
            continue
        if q["id"] in ui_mandate_ids:
            # A mandate is the owner's required step, not tracked debt: packing
            # it is how Q1135 shipped with no artist session. The Quest waits
            # for a human-driven artist/studio review, then a sign-off.
            note(q["id"], "human",
                 "GATE but a UI MANDATE is unmet — packing is refused until the artist "
                 "session is reviewed and signed off (`court artist <quest>` or "
                 "`court studio <ids>`, then `court ui-signoff <quest> --by M'Lord "
                 "--note \"<where said>\"`)",
                 key="ui_mandate_gate")
            continue
        if q["id"] in ui_pending_set:
            # UI-review-pending cannot be packed (`court collect` refuses it).
            # The standing atelier lane below merges it for review; here we just
            # stop offering it to collect so it does not churn a doomed no-op
            # pack every tick.
            note(q["id"], "report",
                 "GATE with UI review PENDING — not collectable; the standing atelier merges it "
                 "for review (merge lane: `--enable ...,atelier`, or `court atelier <ids>` by hand)",
                 key="atelier_pending")
            continue
        role = role_state(q.get("worktree"), "gatekeeper")
        if role["state"] in ("live", "stalled", "watching"):
            continue
        recorded = (q.get("gatekeeper_session_id") or "").strip()
        if recorded:
            cog = (q.get("cogship_id") or "").strip()
            try:
                ahead = int((q.get("git_status") or {}).get("ahead"))
            except (TypeError, ValueError):
                ahead = None
            # A Quest can ship more than once. Once a shipment is promoted, the
            # Quest keeps the old Gatekeeper session (and cogship id) while new
            # work lands on its branch. When that work reaches GATE again, the
            # recorded session belongs to the FINISHED shipment — it is not a
            # duplicate-Gatekeeper risk, so pack a fresh convoy.
            #
            # The promotion is read from the CONVOY's state (resolved from the
            # Quest's cogship_id), NOT from the per-Quest `cogship_promoted_commit`
            # field: that field is written inconsistently, so keying on it alone
            # parked genuinely re-shippable Quests as stale and collapsed the
            # cohort to a singleton (the 2026-11-14 false-stale run).
            promoted = bool(
                (q.get("cogship_promoted_commit") or "").strip()
                or (cog and _norm_cogship(cog) in promoted_cogships)
            )
            if promoted and ahead is not None and ahead > 0:
                note(q["id"], "report",
                     f"GATE carries Gatekeeper {recorded} from the previous shipment "
                     f"({cog or 'prior convoy'}), but {ahead} commit(s) are not "
                     f"in castle — re-shipping in a fresh convoy",
                     key="reship_gate")
            elif promoted and ahead == 0:
                note(q["id"], "human",
                     f"GATE with recorded Gatekeeper {recorded} and the branch is already merged "
                     f"into castle — the prior shipment promoted; advance to READY_TO_RAZE instead "
                     f"of packing a duplicate convoy",
                     key="gate_merged_stuck")
                continue
            else:
                # No verified prior shipment and no live worker: the recorded
                # session is dead. Park it for a human only until the expiry,
                # then clear the dead stamp so the Quest can re-pack — otherwise
                # this hold is permanent and starves the cohort.
                since = float((qstate.get(q["id"]) or {}).get("stale_gatekeeper_since") or 0)
                expiry = float(getattr(policy, "stale_gatekeeper_expiry",
                                       DEFAULT_STALE_GATEKEEPER_EXPIRY) or 0)
                if since and (now - since) >= expiry:
                    note(q["id"], "report",
                         f"GATE with dead Gatekeeper {recorded} and no live worker for "
                         f"{_fmt_window(now - since)} — clearing the stale session stamp so the "
                         f"Quest can re-pack in a fresh convoy",
                         key="stale_gatekeeper_cleared")
                    actions.append(Action(
                        kind="cleargate", quest_ids=[q["id"]],
                        reason=(f"clear dead Gatekeeper session stamp {recorded} on {q['id']} "
                                f"(no live worker; convoy {cog or 'unpromoted'} not promoted)"),
                        meta={"retry_backoff": policy.retry_backoff}))
                else:
                    note(q["id"], "human",
                         f"GATE with recorded Gatekeeper {recorded} but no live worker — "
                         f"triage by hand (regate or clear the session stamp); the watch clears the "
                         f"dead stamp automatically after {_fmt_window(expiry)}",
                         key="stale_gatekeeper")
                continue
        cog = q.get("cogship_id") or ""
        key = cog or "new"
        convoy = convoys.get(key, {})
        # A stale-convoy hold: a prior convoy died mid-integration and left its
        # gatehouse worktree behind. `court collect` would stamp then refuse, so
        # re-collecting is a guaranteed no-op. Escalate once and hold; tick()
        # clears the hold the moment the worktree is torn down.
        stale = convoy.get("stale") if isinstance(convoy.get("stale"), dict) else {}
        if stale:
            wt = stale.get("worktree") or ""
            td_fails = int(convoy.get("teardown_fails") or 0)
            # Opt-in help: if the gatehouse worktree is provably dead (no live
            # process or lease), tear it down so the convoy can be re-collected.
            # Default-on, but never retried forever: after
            # `max_teardown_fails` the convoy is escalated with the exact
            # manual-removal commands instead of looping on a broken git state.
            if (policy.allows("teardown") and wt and key not in teardown_keys
                    and td_fails < policy.max_teardown_fails
                    and now >= float(convoy.get("teardown_retry_at") or 0)
                    and not occupied(wt) and not process_in(wt)):
                teardown_keys.add(key)
                actions.append(Action(
                    kind="teardown", quest_ids=[q["id"]],
                    reason=(f"convoy {key} is dead (stale gatehouse worktree {wt}) — tearing it "
                            f"down so the convoy can re-collect"),
                    meta={"convoy": key, "worktree": wt, "branch": f"the-gatehouse/{key}",
                          "retry_backoff": policy.retry_backoff}))
                continue
            if policy.allows("teardown") and td_fails >= policy.max_teardown_fails:
                note(q["id"], "human",
                     f"convoy {key} stale gatehouse {wt} could not be torn down automatically "
                     f"after {td_fails} attempt(s) ({str(convoy.get('teardown_error') or '')[:140]}) — "
                     f"remove it by hand: `court fork-teardown-list`, then "
                     f"`git -C <repo> worktree remove --force {wt}` and "
                     f"`git branch -D the-gatehouse/{key}`",
                     key="stale_convoy_teardown_failed")
                continue
            note(q["id"], "human",
                 f"convoy {key} is stuck on a stale gatehouse worktree"
                 f"{(' ' + wt) if wt else ''} — a prior convoy died mid-integration; "
                 f"tear it down (or regate) and the watch will re-collect "
                 f"(`court fork-teardown-list`; enable teardown with `--enable ...,teardown`)",
                 key="stale_convoy")
            continue
        fails = int(convoy.get("fails") or 0)
        if fails >= policy.max_fails:
            note(q["id"], "human",
                 f"convoy {key} failed {fails} collect attempt(s) — DEAD LETTER at the "
                 f"{policy.max_fails}-attempt ceiling, retrying only every "
                 f"{_fmt_window(policy.max_retry_backoff)}; needs a Steward look "
                 f"(last error: {str(convoy.get('last_error') or '')[:140]})",
                 key="convoy_fails")
        elif fails:
            note(q["id"], "human",
                 f"convoy {key} failed {fails} collect attempt(s) — needs a Steward look "
                 f"(last error: {str(convoy.get('last_error') or '')[:140]})",
                 key="convoy_fails")
        if now < float(convoy.get("retry_at") or 0):
            continue
        if _within(convoy.get("last_ts"), policy.gate_cooldown, now):
            continue
        by_cogship.setdefault(key, []).append(q["id"])

    for cog, ids in sorted(by_cogship.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if not policy.allows("collect"):
            note(",".join(ids), "skip", "GATE convoy ready but collect is disabled")
            continue
        if cog == "new" and not _collect_cohort_ready(ids, qstate, policy, now):
            note(",".join(ids), "wait",
                 f"{len(ids)} GATE Quest(s) — waiting for a coherent cohort "
                 f"(min {max(1, policy.collect_min_batch)}) or "
                 f"{_fmt_window(policy.collect_batch_window)} before packing",
                 key="collect_cohort:new")
            continue
        # One convoy per tick is enough: a multi-Quest convoy creates a
        # gatehouse, and `court collect` refuses a second one while the first is
        # live. Cap the cohort and leave the remainder for the next tick rather
        # than minting a colliding convoy.
        batch = ids
        if policy.collect_batch_max:
            size = max(1, int(policy.collect_batch_max))
            batch = ids[:size]
            if len(ids) > size:
                note(",".join(ids[size:]), "wait",
                     f"{len(ids) - size} GATE Quest(s) held for the next collect tick "
                     f"(convoy cap {size})", key=f"collect_remainder:{cog}")
        if len(batch) > 1 and gatehouse_slot <= 0:
            note(",".join(batch), "wait",
                 "GATE convoy ready but a gatehouse convoy is already integrating — "
                 "waiting for the next tick", key=f"gatehouse_busy:{cog}")
            continue
        if capacity <= 0:
            note(",".join(batch), "skip",
                 f"GATE convoy ready but worker ceiling reached ({busy_workers}/{policy.max_workers})")
            continue
        actions.append(Action(
            kind="collect",
            quest_ids=batch,
            cogship="" if cog == "new" else cog,
            reason=f"{len(batch)} Quest(s) in GATE awaiting a Gatekeeper",
            meta={"convoy": cog, "retry_backoff": policy.retry_backoff,
                  "noop_backoff": policy.collect_noop_backoff,
                  "stale_backoff": policy.stale_convoy_backoff,
                  "live_backoff": policy.gate_cooldown,
                  "max_retry_backoff": policy.max_retry_backoff},
        ))
        capacity -= 1
        if len(batch) > 1:
            gatehouse_slot -= 1

    # --- Standing atelier: merge UI-review-pending Quests into the studio ----
    # A UI-review-pending GATE Quest is not collectable; it belongs in the ONE
    # long-lived merged studio. `court atelier --standing <ids>` is a local git
    # merge + server ensure (no reasoning worker), so this is a LOCAL action. The
    # single-writer guard inside the command refuses a merge while an artist turn
    # is live, so when one is live we stand down and retry when it is idle.
    # Opt-in (`--enable ...,atelier`); otherwise we only report the standing
    # cohort with the exact manual command.
    if atelier_ready:
        if not policy.allows("atelier"):
            note(",".join(atelier_ready[:3]), "report",
                 f"{len(atelier_ready)} UI-review-pending Quest(s) ready for the standing atelier — "
                 f"merge lane disabled (`--enable ...,atelier`), or merge by hand: "
                 f"`court atelier {','.join(atelier_ready)}`",
                 key="atelier_disabled")
        elif atelier_blocked:
            note(",".join(atelier_ready[:3]), "wait",
                 "artist turn is live in the standing atelier — not merging new Quests into the "
                 "merged view (single-writer); the merge follows when the artist is idle",
                 key="atelier_artist_live")
        else:
            eligible = []
            for qid in atelier_ready:
                st = qstate.get(qid) or {}
                if now < float(st.get("atelier_retry_at") or 0):
                    continue
                if _within(st.get("last_atelier_ts"), policy.atelier_cooldown, now):
                    continue
                eligible.append(qid)
            batch = eligible
            if policy.atelier_batch_max:
                size = max(1, int(policy.atelier_batch_max))
                batch = eligible[:size]
                if len(eligible) > size:
                    note(",".join(eligible[size:]), "wait",
                         f"{len(eligible) - size} UI Quest(s) held for the next atelier merge tick "
                         f"(batch cap {size})", key="atelier_remainder")
            if batch:
                actions.append(Action(
                    kind="atelier", quest_ids=batch,
                    reason=(f"{len(batch)} UI-review-pending Quest(s) merged into the standing "
                            f"atelier for review"),
                    meta={"retry_backoff": policy.retry_backoff,
                          "cooldown": policy.atelier_cooldown}))

    # --- Dead gatehouse sweep (independent of any GATE Quest) ---------------
    # A dead `the-gatehouse/*` worktree blocks future collects whose allocated
    # cogship id collides with it, and it silently fills the disk. The stale
    # hold above only exists after a collect has already failed; this sweep
    # revives the common case proactively. A worktree is only eligible when
    # `court status --json` reported no fresh session AND no lease/process owns
    # it, so a live Gatekeeper is never torn down.
    planned_teardowns = {a.meta.get("worktree") for a in actions if a.kind == "teardown"}
    if policy.allows("teardown"):
        for gh in snap.get("gatehouses") or []:
            wt = gh.get("path") or ""
            key = gh.get("cogship_id") or "new"
            if not wt or wt in planned_teardowns or gh.get("fresh_session"):
                continue
            if int(gh.get("age_seconds") or 0) < policy.gatehouse_grace:
                continue
            if occupied(wt) or process_in(wt):
                continue
            convoy = convoys.get(key) or {}
            fails = int(convoy.get("teardown_fails") or 0)
            if fails >= policy.max_teardown_fails:
                note(key, "human",
                     f"dead gatehouse {wt} could not be torn down automatically after "
                     f"{fails} attempt(s) ({str(convoy.get('teardown_error') or '')[:140]}) — remove "
                     f"it by hand: `git -C <repo> worktree remove --force {wt}`",
                     key="gatehouse_teardown_failed")
                continue
            if now < float(convoy.get("teardown_retry_at") or 0):
                continue
            # Teardown is a local git operation, not a model worker: it must not
            # be starved by the live-worker ceiling. It is still bounded by
            # `max_spawns_per_tick` with every other action at the end of plan().
            actions.append(Action(
                kind="teardown", quest_ids=[],
                reason=(f"gatehouse {key} has no fresh session and no live worker — "
                        f"reviving the convoy lane by removing {wt}"),
                meta={"convoy": key, "worktree": wt,
                      "branch": gh.get("branch") or f"the-gatehouse/{key}",
                      "retry_backoff": policy.retry_backoff}))
            planned_teardowns.add(wt)

    # --- TRIBUTE_READY: send in the Master of Coin -------------------------
    for q in quests.values():
        if q.get("status") != "TRIBUTE_READY":
            continue
        if q.get("pending_audience"):
            note(q["id"], "skip", "TRIBUTE_READY but pending Audience — parked")
            continue
        if q["id"] in rebase_ids:
            note(q["id"], "wait", "rebasing onto castle this tick — coin follows next tick")
            continue
        if not q.get("worktree") or not Path(q["worktree"]).is_dir():
            note(q["id"], "human", "TRIBUTE_READY with no usable worktree")
            continue
        st = qstate.get(q["id"]) or {}
        mc = st.get("merge_conflict")
        if isinstance(mc, dict):
            # A spawn blocked by a rebase conflict is held for a Steward
            # decision. Once the muster has gone out (escalated_ts), the hold
            # becomes a PROBE: after the retry backoff the coin is attempted
            # again — a Steward resolution (or a remediation branching) that
            # lands makes it succeed, clearing the marker and resuming the
            # pipeline; a still-conflicted attempt re-stamps the marker and
            # holds again. Without this, a resolved conflict would stay held
            # forever because the marker only clears on a successful spawn.
            if not mc.get("escalated_ts") or now - float(mc.get("ts") or 0) < policy.retry_backoff:
                files = ", ".join((mc.get("files") or [])[:6]) or "unknown files"
                level = "wait" if policy.allows("steward") else "human"
                note(q["id"], level,
                     f"TRIBUTE_READY but `git merge castle` conflicts in {files} — held for a "
                     f"{'Steward decision' if policy.allows('steward') else 'human/Steward resolution'}"
                     + (" (probe pending after backoff)" if mc.get("escalated_ts") else ""),
                     key="merge_conflict")
                continue
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
        if q["id"] in rebase_ids:
            note(q["id"], "wait", "rebasing onto castle this tick — goad follows next tick")
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
        mc = st.get("merge_conflict")
        if isinstance(mc, dict):
            # Same probe semantics as the TRIBUTE_READY hold: held while the
            # conflict is new; once the Steward muster has gone out, re-probe
            # GOAD after the backoff so a resolved conflict clears the marker.
            if not mc.get("escalated_ts") or now - float(mc.get("ts") or 0) < policy.retry_backoff:
                files = ", ".join((mc.get("files") or [])[:6]) or "unknown files"
                level = "wait" if policy.allows("steward") else "human"
                note(q["id"], level,
                     f"WORKING but `git merge castle` conflicts in {files} — held for a "
                     f"{'Steward decision' if policy.allows('steward') else 'human/Steward resolution'}"
                     + (" (probe pending after backoff)" if mc.get("escalated_ts") else ""),
                     key="merge_conflict")
                continue
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

    # --- Merge-conflict escalation: a Steward picks the resolution course ----
    # A coin/goad spawn blocked by `git merge castle` is a deterministic
    # blocker — retrying cannot clear it. Those quests are held above; the
    # Steward must decide the course (branch-wins charter vs. castle-wins
    # ledger, a remediation Serf, or a human). Escalate PROMPTLY (not gated
    # on board staleness or zero workers) but still bounded: one muster per
    # conflict marker, steward cooldown and daily cap respected, and never
    # stacked on a live steward session.
    conflict_quests = [
        (qid, st) for qid, st in qstate.items()
        if isinstance(st.get("merge_conflict"), dict)
        and not st["merge_conflict"].get("escalated_ts")
        and qid in quests
        and (quests[qid].get("status") or "").upper() in ("WORKING", "QUESTING", "TRIBUTE_READY")
    ]
    if conflict_quests and policy.allows("steward"):
        steward_state = state.get("steward") or {}
        recent = [float(t) for t in steward_state.get("spawns", [])
                  if (now - float(t)) < 86400]
        steward_live = bool((workers.get("by_role") or {}).get("steward")) or (
            (workers.get("steward_lease") or {}).get("state") in ("live", "stalled", "watching"))
        if not steward_live and not any(a.kind == "steward" for a in actions):
            if now < float(steward_state.get("retry_at") or 0):
                pass
            elif _within(steward_state.get("last_ts"), policy.steward_cooldown, now):
                note(",".join(qid for qid, _ in conflict_quests[:3]), "wait",
                     "merge conflicts persist but a Steward was mustered recently")
            elif len(recent) >= max(1, policy.max_stewards_per_day):
                note(",".join(qid for qid, _ in conflict_quests[:3]), "human",
                     f"{len(conflict_quests)} merge-conflict blocker(s) and the daily Steward "
                     f"muster cap is spent ({len(recent)}/{policy.max_stewards_per_day}) — needs a human",
                     key="merge_conflict_steward_cap")
            elif capacity <= 0:
                note(",".join(qid for qid, _ in conflict_quests[:3]), "skip",
                     f"merge conflicts need a Steward but worker ceiling "
                     f"reached ({busy_workers}/{policy.max_workers})")
            else:
                files = sorted({f for _, st in conflict_quests for f in (st["merge_conflict"].get("files") or [])})
                actions.append(Action(
                    kind="steward", quest_ids=[qid for qid, _ in conflict_quests],
                    reason=(f"`git merge castle` conflicts block {'/'.join(qid for qid, _ in conflict_quests[:3])}"
                            + (f" (+{len(conflict_quests) - 3} more)" if len(conflict_quests) > 3 else "")
                            + f" in: {', '.join(files)[:240]} — Steward picks the resolution course"),
                    meta={"retry_backoff": policy.retry_backoff, "merge_conflict": True}))
                # Persist the escalation in the one-shot `notified` bookkeeping so
                # "escalated once" is actually recorded (the marker's
                # `escalated_ts` is stamped on muster SUCCESS; this records the
                # decision to escalate even if the muster is deferred). Without a
                # note the conflict path left no trace in `notified` at all.
                note(",".join(qid for qid, _ in conflict_quests[:3]), "wait",
                     f"mustering a Steward for {len(conflict_quests)} merge-conflict blocker(s) "
                     f"— need a resolution course",
                     key="merge_conflict_escalation")
                capacity -= 1

    # --- Last resort: muster ONE Steward triage session ---------------------
    # Everything above is a mechanical verb the watch can issue itself. When
    # the board has been unchanged for `steward_stale_after`, NOTHING is
    # running, and a standing blocker remains that only a Steward can decide,
    # the watch may bring up one single-turn Steward session to unstick it.
    # Opt-in (`--enable ...,steward`), cooldown-capped, and daily-capped.
    if policy.allows("steward") and quests:
        sig = board_signature(quests, notes)
        prev_since = float(state.get("board_sig_since") or 0)
        since = prev_since if (state.get("board_signature") == sig and prev_since) else now
        stale_for = max(0.0, now - since)
        steward_state = state.get("steward") or {}
        recent = [float(t) for t in steward_state.get("spawns", [])
                  if (now - float(t)) < 86400]
        steward_live = bool((workers.get("by_role") or {}).get("steward")) or (
            (workers.get("steward_lease") or {}).get("state") in ("live", "stalled", "watching"))
        active = [q for q in quests.values() if q.get("status") in ACTIVE_STAGES]
        blocker = any(n.get("level") == "human" for n in notes) or any(
            q.get("status") in STEWARD_ONLY_STAGES for q in active)
        if (active and blocker and not steward_live and not actions
                and workers.get("count", 0) == 0
                and stale_for >= policy.steward_stale_after):
            if now < float(steward_state.get("retry_at") or 0):
                pass
            elif _within(steward_state.get("last_ts"), policy.steward_cooldown, now):
                note("board", "wait",
                     f"board unchanged for {int(stale_for // 60)}m but a Steward was mustered recently")
            elif len(recent) >= max(1, policy.max_stewards_per_day):
                note("board", "human",
                     f"board unchanged for {int(stale_for // 60)}m and the daily Steward muster cap is "
                     f"spent ({len(recent)}/{policy.max_stewards_per_day}) — needs a human",
                     key="steward_cap")
            elif capacity <= 0:
                note("board", "skip",
                     f"board unchanged for {int(stale_for // 60)}m but worker ceiling "
                     f"reached ({busy_workers}/{policy.max_workers})")
            else:
                blocked = sum(1 for n in notes if n.get("level") == "human")
                actions.append(Action(
                    kind="steward", quest_ids=[],
                    reason=(f"board unchanged for {int(stale_for // 60)}m with no live worker and "
                            f"{blocked or len(active)} standing blocker(s) — mustering a Steward triage turn"),
                    meta={"stale_for": int(stale_for), "board_signature": sig,
                          "retry_backoff": policy.retry_backoff}))
                capacity -= 1

    # --- raze / regate (automatic; razing is no longer human-gated) ---------
    # A READY_TO_RAZE Quest has already had its promotion verified, so the
    # remaining work is mechanical: tear the worktree down (raze), or — when
    # `court raze`'s ancestor check fails because the convoy was squashed —
    # hand the Quest back to a fresh convoy (regate) first. Neither needs a
    # human decision; both are bounded (cooldown, regate attempts) and both
    # escalate to a Steward only after the mechanical path is exhausted.
    regate_ids = set(raze_regate or ())
    # Regates are batched by app: handing two same-app Quests back in one fresh
    # convoy re-runs the unified suite once instead of once per Quest. The
    # per-Quest cooldown/attempt gates are still evaluated below, so a Quest
    # already in backoff is simply left out of its cohort.
    regate_batches: dict[str, list[str]] = {}
    for q in quests.values():
        status = q.get("status")
        if status in ("PLANNED", "CHARTERED", "DISPATCHED"):
            note(q["id"], "report", f"{status} — waiting on a Steward dispatch decision")
        elif status == "READY_TO_RAZE":
            if q.get("kind") == "epic":
                note(q["id"], "report", "READY_TO_RAZE epic — raze via the Steward")
                continue
            if q.get("pending_audience"):
                note(q["id"], "skip", "READY_TO_RAZE but pending Audience — parked")
                continue
            if q.get("worktree") and (occupied(q.get("worktree")) or process_in(q.get("worktree"))):
                note(q["id"], "wait", "READY_TO_RAZE but a worker is still live in its worktree")
                continue
            if q["id"] in ui_mandate_ids:
                # Razing tears the worktree down; that is exactly where the
                # mandated artist session has to run. Keep it and escalate.
                note(q["id"], "human",
                     "READY_TO_RAZE but a UI MANDATE is unmet — razing would destroy the "
                     "worktree the artist session needs. Run `court artist <quest>` / "
                     "`court studio <ids>`, record `court ui-signoff <quest> --by M'Lord "
                     "--note \"<where said>\"`, then raze",
                     key="ui_mandate_raze")
                continue
            st = qstate.get(q["id"]) or {}
            if q.get("razed_at"):
                # Promotion was verified in an earlier pass; only the teardown
                # is outstanding. `court raze` is idempotent for this state.
                if not policy.allows("raze"):
                    note(q["id"], "report",
                         f"READY_TO_RAZE and already razed at {q['razed_at']} — worktree "
                         f"awaits teardown (`--enable ...,raze`)", key="razed_pruned")
                    continue
                if _within(st.get("last_raze_ts"), policy.raze_cooldown, now):
                    continue
                actions.append(Action(
                    kind="raze", quest_ids=[q["id"]],
                    reason=f"already razed at {q['razed_at']} — running the outstanding teardown",
                    meta={"retry_backoff": policy.retry_backoff}))
                continue
            if q["id"] in regate_ids:
                if not policy.allows("regate"):
                    note(q["id"], "report",
                         "READY_TO_RAZE but the branch tip is not an ancestor of castle — "
                         "regate needed (`--enable ...,regate`)")
                    continue
                fails = int(st.get("regate_fails") or 0)
                if fails >= policy.max_regates:
                    note(q["id"], "human",
                         f"READY_TO_RAZE and raze refuses (branch tip not an ancestor of "
                         f"castle); {fails} regate attempt(s) failed — the convoy needs a "
                         f"Steward", key="regate_exhausted")
                    continue
                if _within(st.get("last_regate_ts"), policy.regate_cooldown, now):
                    continue
                regate_batches.setdefault(q.get("app") or "unknown", []).append(q["id"])
                continue
            if not policy.allows("raze"):
                note(q["id"], "report",
                     "READY_TO_RAZE — promotion verified; teardown/raze is disabled "
                     "(`--enable ...,raze`)")
                continue
            if _within(st.get("last_raze_ts"), policy.raze_cooldown, now):
                continue
            actions.append(Action(
                kind="raze", quest_ids=[q["id"]],
                reason="READY_TO_RAZE — promotion verified; razing and tearing down the worktree",
                meta={"retry_backoff": policy.retry_backoff}))
        elif status == "HELD":
            note(q["id"], "report", "HELD — awaiting Audience")

    # One fresh convoy per app cohort, largest first. A multi-Quest regate
    # creates a gatehouse, so at most one such cohort (across collect + regate)
    # is planned per tick; size-1 regates run in the Quest worktree and are
    # exempt. The remainder of a capped cohort waits for the next tick.
    max_batch = max(1, int(policy.regate_batch_max or 1))
    for app, ids in sorted(regate_batches.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        chunk = ids[:max_batch]
        if len(ids) > len(chunk):
            note(",".join(ids[len(chunk):]), "wait",
                 f"{len(ids) - len(chunk)} READY_TO_RAZE Quest(s) for {app} held for the "
                 f"next regate tick (cohort cap {max_batch})",
                 key=f"regate_remainder:{app}")
        if len(chunk) > 1 and gatehouse_slot <= 0:
            note(",".join(chunk), "wait",
                 "regate cohort ready but a gatehouse convoy is already integrating — "
                 "waiting for the next tick", key=f"regate_gatehouse_busy:{app}")
            continue
        actions.append(Action(
            kind="regate", quest_ids=chunk,
            reason=(f"{len(chunk)} READY_TO_RAZE Quest(s) for {app} refused by raze "
                    f"(branch tip not an ancestor of castle) — re-gating one fresh convoy"),
            meta={"retry_backoff": policy.retry_backoff, "cohort": app}))
        if len(chunk) > 1:
            gatehouse_slot -= 1

    # The per-tick cap bounds WORKER spawns, not local-git/bookkeeping actions.
    # Capping the flat list let a queue of zero-cost `rebase`/`advance`/`note`
    # vigils (appended ahead of the heuristics) consume every slot and starve
    # the dispatch/coin/collect actions that actually move the pipeline — and
    # the watch's own drift-rebase lane with them. Non-spawning actions pass
    # through; only spawning ones are capped at `max_spawns_per_tick`.
    #
    # The budget is spent by CLASS, not by append order. With a small budget the
    # one or two slots go to whichever action happens to be first, and Steward-
    # enqueued `dispatch` hand-offs are planned ahead of the drain heuristics, so
    # a vigil backlog starves collect/coin exactly as it starves the worker-slot
    # pool above. Spend the budget drain-first (the pipeline's in-flight exit
    # path) and reserve one slot for every other class that has work waiting, so
    # neither in-flight work nor new work is shut out while the other has a
    # backlog. The surviving spawning actions keep their original plan order.
    # Spend the per-tick spawn budget FAIRLY ACROSS LANES, not by append order.
    # With a small budget the one or two slots went to whichever action was
    # appended first, and the queue-driven `dispatch`/vigil hand-offs are appended
    # ahead of the collect/coin heuristics — so a backlog in one lane starved
    # every other lane all night (2026-11-13: 16 TRIBUTE_READY and 5 GATE with no
    # coin/collect action for hours). Slots now take turns: one action per class
    # per round, classes ordered by a mild drain bias and ROTATED each time the
    # budget binds, so consecutive ticks lead with different lanes and the
    # pipeline stays balanced instead of merely flipping which lane starves.
    # Non-spawning (local) actions are unbudgeted and always pass through.
    #
    # The same allocation now also respects the ROLLING HOURLY budget: planning a
    # spawn the hour cannot pay for only produces an execution-time skip (and,
    # because the skip loop breaks, silently drops every spawn action behind it)
    # while the drain lanes are still not served. The per-tick budget is
    # therefore capped by the hour's remaining slots, the reserved drain share is
    # never handed to a non-drain lane, and the retained actions are ordered so
    # the leading lane spends first.
    _tick_budget = max(0, policy.max_spawns_per_tick)
    _budget = _tick_budget if hourly_remaining is None else min(_tick_budget, hourly_remaining)
    _spawn_slots = [_i for _i, _a in enumerate(actions) if _action_spawns(_a)]
    _queues: dict = {}
    for _i in _spawn_slots:
        _queues.setdefault(_spawn_class(actions[_i]), []).append(_i)
    # Gate-short precedence: while the gate holds fewer packable Quests than a
    # full convoy, the coin lane (TRIBUTE_READY -> GATE) is the binding
    # constraint on convoy fullness, so it takes strict precedence over the
    # new-work/repair lanes (goad/dispatch): it leads every tick and the fairness
    # rotation is suspended until either the gate fills or the coin lane empties.
    # Every other tick the drain bias and rotation are unchanged.
    _coin_priority = gate_short and "coin" in _queues
    _lanes = sorted(
        _queues,
        key=lambda _c: (
            -1 if (_coin_priority and _c == "coin") else (0 if _c in DRAIN_KINDS else 1),
            _c,
        ),
    )
    _rotation = int(state.get("spawn_rotation") or 0)
    if len(_lanes) > 1 and not _coin_priority:
        _shift = _rotation % len(_lanes)
        _lanes = _lanes[_shift:] + _lanes[:_shift]
    # Drain reservation inside the hour: with the reserved share still unspent,
    # non-drain lanes may only take the slots left after it. Applied only when a
    # drain action is actually waiting, so a temporarily unplannable drain lane
    # (a live Master of Coin, a cooldown) never idles the hour's budget.
    _non_drain_cap = None
    _drain_waiting = any(_spawn_class(actions[_i]) in DRAIN_KINDS for _i in _spawn_slots)
    if drain_pending and drain_reserve_left and _drain_waiting:
        # How many more non-drain spawns the hour can still afford while the
        # reserved drain share stays protected. Deliberately derived from the
        # HOUR's remaining slots (not the per-tick budget), so the reservation
        # only binds as the hour runs out instead of throttling every tick.
        _affordable = _budget if hourly_remaining is None else hourly_remaining
        _non_drain_cap = max(0, _affordable - drain_reserve_left)
    _kept: set = set()
    _cursor: dict = {_c: 0 for _c in _queues}
    _non_drain_kept = 0
    while len(_kept) < _budget:
        _progressed = False
        for _c in _lanes:
            if len(_kept) >= _budget:
                break
            while _cursor[_c] < len(_queues[_c]):
                _i = _queues[_c][_cursor[_c]]
                _cursor[_c] += 1
                if (_non_drain_cap is not None and _c not in DRAIN_KINDS
                        and _non_drain_kept >= _non_drain_cap):
                    # The hour's remaining slots are reserved for drain work.
                    continue
                if _c not in DRAIN_KINDS:
                    _non_drain_kept += 1
                _kept.add(_i)
                _progressed = True
                break
        if not _progressed:
            break
    if len(_kept) < len(_spawn_slots):
        # The budget bound (per-tick OR hourly), so fairness was at stake:
        # advance the rotation so the next tick leads with a different lane.
        state["spawn_rotation"] = _rotation + 1
    _lane_pos = {_c: _k for _k, _c in enumerate(_lanes)}

    def _spend_key(_i: int):
        """Order retained spawn actions: drain lanes first while drain work is
        pending (the pipeline's in-flight exit path must never lose the hour's
        last slot to a hand-off that merely happened to be appended earlier),
        then the rotated lane order."""
        _c = _spawn_class(actions[_i])
        return (0 if (drain_pending and _c in DRAIN_KINDS) else 1,
                _lane_pos.get(_c, len(_lanes)), _i)

    _spend_order = iter(sorted([_i for _i in _spawn_slots if _i in _kept], key=_spend_key))
    _ordered: list = []
    for _i, _a in enumerate(actions):
        if _action_spawns(_a):
            if _i in _kept:
                _ordered.append(actions[next(_spend_order)])
            continue
        _ordered.append(_a)
    actions = _ordered

    # One-free-slot drain bookkeeping: count consecutive ticks that had a free
    # slot (at most one) and spent it on a non-drain spawn, so the next tick
    # holds that slot for the drain lanes. A hold that produced no drain action
    # (nothing plannable) is released immediately so dispatch is never parked
    # behind a drain lane that cannot use the slot. Only a real change is
    # written: plan() stays a pure read for a board where nothing is deferred.
    if drain_pending:
        _drain_scheduled = any(_action_spawns(_a) and _spawn_class(_a) in DRAIN_KINDS
                               for _a in actions)
        if _drain_scheduled or one_slot_drain_hold:
            if state.get("drain_slot_deferrals"):
                state["drain_slot_deferrals"] = 0
        elif capacity_at_start <= 1 and _budget and any(_action_spawns(_a) for _a in actions):
            state["drain_slot_deferrals"] = int(state.get("drain_slot_deferrals") or 0) + 1

    # --- Session-store razing (maintenance, never work) ---------------------
    # The Kilo session/event store grows without bound (15 GB by 2026-11-08).
    # Razing is deliberately appended AFTER the spawn budget: it is mechanical
    # housekeeping, not a worker. It is only eligible on a genuinely idle board
    # — no live worker anywhere and no worker-spawning action this tick — since
    # its VACUUM takes an exclusive lock that would fail an in-flight write.
    # At most once per `session_prune_interval`; retention and event-window are
    # policy, so a quiet machine ages its own history without a human cron.
    if policy.allows("prune"):
        maint = state.get("maintenance") or {}
        last = float(maint.get("session_prune_ts") or 0)
        spawning = any(a.kind in ("goad", "coin", "collect", "dispatch", "steward", "prune")
                       for a in actions)
        if (busy_workers == 0 and not spawning
                and (now - last) >= policy.session_prune_interval
                and now >= float(maint.get("session_prune_retry_at") or 0)):
            actions.append(Action(
                kind="prune", quest_ids=[],
                reason=(f"machine idle — razing sessions older than "
                        f"{policy.session_retention_days}d and vacuuming the store"),
                meta={"days": policy.session_retention_days,
                      "keep_events": policy.session_keep_events,
                      "vacuum": policy.session_prune_vacuum,
                      "retry_backoff": policy.retry_backoff}))

    # --- pytest-database reaping (maintenance, never work) ------------------
    # Every ephemeral gatehouse worktree hashes a new BASE_DIR, so its pytest
    # databases are new; `--reuse-db` never drops the previous set. 964 of them
    # (~34 GB) had accumulated by 2026-11-09. Dropping a database is only safe
    # while nothing is running, so this is idle-board gated like the session
    # razing above, and interval-bounded so it is a background sweep rather
    # than a per-tick query.
    if policy.allows("pgprune"):
        maint = state.get("maintenance") or {}
        last = float(maint.get("pg_prune_ts") or 0)
        spawning = any(a.kind in ("goad", "coin", "collect", "dispatch", "steward",
                                  "regate", "prune", "pgprune")
                       for a in actions)
        if (state.get("supervisor_started") and busy_workers == 0 and not spawning
                and (now - last) >= policy.pg_prune_interval
                and now >= float(maint.get("pg_prune_retry_at") or 0)):
            actions.append(Action(
                kind="pgprune", quest_ids=[],
                reason="machine idle — dropping pytest databases whose worktree no longer exists",
                meta={"retry_backoff": policy.retry_backoff}))

    return actions, notes


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _prune_spawns(state: dict, now: float) -> list:
    """Drop spawn stamps older than the rolling hour and return the survivors.

    Keeps the drain-lane subset (`drain_spawns`) in step: plan() settles the
    hourly reserve against it, so a stale stamp would hold a reservation that no
    longer applies."""
    spawns = [float(t) for t in state.get("spawns", []) if (now - float(t)) < SPAWN_WINDOW_SECONDS]
    state["spawns"] = spawns
    state["drain_spawns"] = [
        float(t) for t in state.get("drain_spawns", [])
        if (now - float(t)) < SPAWN_WINDOW_SECONDS
    ]
    return spawns


def _record_spawn(state: dict, action: Action, now: float) -> None:
    """Charge one executed worker spawn against the hourly budgets.

    `spawns` is the rolling hourly total; `drain_spawns` is the collect/coin
    subset the hourly drain reservation is settled from. Only a worker-spawning
    action is charged — a local `rebase`/`advance` costs no spawn budget."""
    state.setdefault("spawns", []).append(now)
    if _spawn_class(action) in DRAIN_KINDS:
        state.setdefault("drain_spawns", []).append(now)


def _execute_cleargate(repo: Path, action: Action, timeout: int = 60) -> dict:
    """Clear a dead Gatekeeper session stamp on a GATE Quest so it can re-pack.

    The stamp is a duplicate-Gatekeeper guard, but when the worker is gone and
    the convoy was never promoted it strands the Quest: plan() holds it forever
    (the "stale_gatekeeper" dead end). After `stale_gatekeeper_expiry` the watch
    clears the dead stamp itself. Bounded to the named Quests; clears only the
    session/model stamps, never the branch or cogship id.
    """
    try:
        from . import store
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "exit": -1, "argv": [], "stdout_tail": "",
                "stderr_tail": f"store unavailable: {e}", "seconds": 0.0}
    started = time.time()
    cleared: list = []
    errors: list = []
    for qid in action.quest_ids:
        try:
            quest = store.load(qid, court_root=Path(repo) / ".court")
            quest.gatekeeper_session_id = ""
            quest.gatekeeper_model = ""
            store.save(quest, auto_commit=True,
                       commit_msg=f"court: clear dead Gatekeeper session on {qid}")
            cleared.append(qid)
        except Exception as e:  # noqa: BLE001 — one bad Quest must not stop the rest
            errors.append(f"{qid}: {e}")
    ok = not errors
    return {"ok": ok, "exit": 0 if ok else 1, "argv": [],
            "stdout_tail": "cleared dead Gatekeeper stamp on " + ", ".join(cleared) if cleared else "",
            "stderr_tail": "; ".join(errors)[:500],
            "seconds": round(time.time() - started, 1)}


def _execute_teardown(repo: Path, action: Action, timeout: int = 120) -> dict:
    """Remove a dead convoy's gatehouse worktree so `collect` can re-create it.

    Destructive but tightly scoped: only ever called for a worktree plan() has
    proven stale AND has confirmed has no live worker/lease (and only for a
    path under `<repo>/.kilo/worktrees/the-gatehouse-*`). Removes the git
    worktree and deletes its `the-gatehouse/<cogship>` branch.

    Revival ladder: `git worktree remove --force`, then `git worktree prune`
    (a half-deleted registration is the usual reason a remove fails), then a
    guarded directory delete when the worktree registration is gone but the
    directory survived. `collect` only checks that the directory does not
    exist, so leaving an unregistered directory behind blocks the convoy
    forever — exactly the cogship-381/399 class."""
    wt = str(action.meta.get("worktree") or "")
    branch = str(action.meta.get("branch") or "")
    started = time.time()
    if not wt or not Path(wt).is_dir():
        return {"ok": True, "exit": 0, "argv": [],
                "stdout_tail": "gatehouse worktree already gone", "stderr_tail": "",
                "seconds": 0.0}

    gatehouse_root = (Path(repo) / ".kilo" / "worktrees").resolve()
    try:
        wt_path = Path(wt).resolve()
    except OSError:
        wt_path = Path(wt)
    if gatehouse_root not in wt_path.parents or not wt_path.name.startswith("the-gatehouse-"):
        return {"ok": False, "exit": -1, "argv": [],
                "stdout_tail": "",
                "stderr_tail": f"refusing to tear down non-gatehouse path {wt}",
                "seconds": round(time.time() - started, 1)}

    outs = []
    remove_cmd = ["git", "-C", str(repo), "worktree", "remove", "--force", wt]
    try:
        res = subprocess.run(remove_cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "exit": -1, "argv": remove_cmd, "stdout_tail": "",
                "stderr_tail": str(e)[:500], "seconds": round(time.time() - started, 1)}
    if res.returncode != 0:
        # A stale registration (the directory was already replaced/partially
        # deleted by hand) makes `worktree remove` refuse. Prune the stale
        # metadata, then clear the directory if it survived.
        prune_cmd = ["git", "-C", str(repo), "worktree", "prune"]
        try:
            subprocess.run(prune_cmd, capture_output=True, text=True, timeout=timeout)
        except Exception:  # noqa: BLE001
            pass
        if Path(wt).is_dir():
            try:
                shutil.rmtree(Path(wt))
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "exit": res.returncode, "argv": remove_cmd,
                        "stdout_tail": (res.stdout or "")[-1200:],
                        "stderr_tail": ((res.stderr or "") + f"\nrmtree failed: {e}")[-1200:],
                        "seconds": round(time.time() - started, 1)}
        outs.append(f"pruned stale registration and removed {wt}")
    else:
        outs.append((res.stdout or "").strip())

    # Belt-and-suspenders: `worktree remove` can return 0 yet leave the
    # directory (e.g. a partially-removed registration). `collect` only checks
    # that the directory doesn't exist, so clear any survivor.
    if Path(wt).is_dir():
        try:
            subprocess.run(["git", "-C", str(repo), "worktree", "prune"],
                           capture_output=True, text=True, timeout=timeout)
            shutil.rmtree(Path(wt))
            outs.append(f"removed surviving gatehouse directory {wt}")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "exit": -1, "argv": remove_cmd,
                    "stdout_tail": "\n".join(outs)[-1200:],
                    "stderr_tail": f"directory survived remove and could not be cleared: {e}"[:1200],
                    "seconds": round(time.time() - started, 1)}

    if branch:
        del_cmd = ["git", "-C", str(repo), "branch", "-D", branch]
        try:
            res = subprocess.run(del_cmd, capture_output=True, text=True, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "exit": -1, "argv": del_cmd, "stdout_tail": "",
                    "stderr_tail": str(e)[:500], "seconds": round(time.time() - started, 1)}
        if res.returncode != 0 and Path(wt).is_dir():
            return {"ok": False, "exit": res.returncode, "argv": del_cmd,
                    "stdout_tail": (res.stdout or "")[-1200:],
                    "stderr_tail": (res.stderr or "")[-1200:],
                    "seconds": round(time.time() - started, 1)}
        outs.append((res.stdout or "").strip())
    return {"ok": not Path(wt).is_dir(), "exit": 0,
            "argv": [remove_cmd[-1]], "stdout_tail": "\n".join(outs)[-1200:],
            "stderr_tail": "", "seconds": round(time.time() - started, 1)}


def _execute_prune(repo: Path, action: Action, timeout: int = SESSION_PRUNE_TIMEOUT) -> dict:
    """Raze old Kilo sessions, then reclaim the file.

    Two idempotent steps through the verified ``court db-prune`` front door:
    ``prune --apply`` (chunked, share-safe deletes) followed by
    ``vacuum --apply`` (the only step that actually shrinks the file). A
    partial success is a failure — the interval only advances when both land,
    so a locked/refused vacuum is retried on the next idle window.
    """
    days = int(action.meta.get("days") or DEFAULT_SESSION_RETENTION_DAYS)
    keep = int(action.meta.get("keep_events") or DEFAULT_SESSION_KEEP_EVENTS)
    started = time.time()
    outs: list[str] = []
    errs: list[str] = []
    steps = [["db-prune", "prune", "--days", str(days),
              "--keep-events", str(keep), "--apply"]]
    if action.meta.get("vacuum", True):
        steps.append(["db-prune", "vacuum", "--apply"])
    ok = True
    for argv in steps:
        try:
            res = _court(repo, *argv, timeout=timeout)
        except subprocess.TimeoutExpired:
            errs.append(f"{' '.join(argv)} timed out after {timeout}s")
            ok = False
            break
        except Exception as e:  # noqa: BLE001 — a supervisor must never die on one action
            errs.append(f"{' '.join(argv)} failed: {e}")
            ok = False
            break
        outs.append((res.stdout or "").strip())
        errs.append((res.stderr or "").strip())
        if res.returncode != 0:
            ok = False
            break
    return {"ok": ok, "exit": 0 if ok else 1, "argv": steps[-1],
            "stdout_tail": "\n".join(o for o in outs if o)[-1200:],
            "stderr_tail": "\n".join(e for e in errs if e)[-1200:],
            "seconds": round(time.time() - started, 1)}


def _execute_pgprune(repo: Path, action: Action, timeout: int = PRUNE_TIMEOUT) -> dict:
    """Reap pytest databases whose worktree is gone (regenerable, ~6 s to rebuild)."""
    started = time.time()
    try:
        res = _court(repo, "prune-test-dbs", "--apply", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit": -1, "argv": ["prune-test-dbs", "--apply"],
                "stdout_tail": "", "stderr_tail": f"timed out after {timeout}s",
                "seconds": round(time.time() - started, 1)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "exit": -1, "argv": ["prune-test-dbs", "--apply"],
                "stdout_tail": "", "stderr_tail": str(e)[:500],
                "seconds": round(time.time() - started, 1)}
    out = (res.stdout or "").strip()
    err = (res.stderr or "").strip()
    plan = action.meta or {}
    return {"ok": res.returncode == 0, "exit": res.returncode,
            "argv": ["prune-test-dbs", "--apply"],
            "stdout_tail": out[-1200:], "stderr_tail": err[-1200:],
            "seconds": round(time.time() - started, 1), "plan": plan}


def execute(repo: Path, action: Action, timeout: int = DEFAULT_ACTION_TIMEOUT) -> dict:
    if action.kind == "teardown":
        return _execute_teardown(repo, action)
    if action.kind == "cleargate":
        return _execute_cleargate(repo, action)
    if action.kind == "prune":
        return _execute_prune(repo, action)
    if action.kind == "pgprune":
        return _execute_pgprune(repo, action)
    if action.kind == "vigil":
        then = action.meta.get("then") or ""
        argv, _spawns = vigils.action_argv(
            then, {"then": then, "brief": action.meta.get("brief") or ""})
        if not argv:
            # `note:<text>` is bookkeeping only — there is no command to run.
            # Every OTHER non-spawning vigil (`rebase`, `advance`) builds a real
            # argv and MUST execute. The old early-return for `spawns: false`
            # synthesised `ok/exit 0` without running anything, so a queued
            # `rebase:<QID>` vigil burned its fires green and converged nothing
            # (and, being appended ahead of the heuristics, starved the watch's
            # own rebase lane at the per-tick cap).
            return {"ok": True, "exit": 0, "argv": [], "stdout_tail": f"note-only vigil: {then}",
                    "stderr_tail": "", "seconds": 0.0}
    elif action.kind == "charter":
        argv = ["charter-request", *action.quest_ids]
    elif action.kind == "regate":
        # A raze the gate refused (branch tip not an ancestor of castle) goes
        # back through a fresh convoy instead of waiting for a human. `collect`
        # takes ONE positional that may be a comma-separated cohort, so the
        # batch rides in one argv entry (N positionals are rejected).
        argv = ["collect", ",".join(action.quest_ids), "--regate", "--standup"]
    elif action.kind == "collect":
        # `court collect` takes ONE positional that may be a comma-separated
        # list (quest_ids, batchable) — passing N positionals makes argparse
        # reject the extras ("unrecognized arguments"), which silently broke
        # every multi-quest convoy the watch tried to pack.
        argv = ["collect"] + ([",".join(action.quest_ids)] if action.quest_ids else [])
    elif action.kind == "dispatch":
        # A fresh dispatch (the charter pickup lane) spawns a NEW worktree from
        # the castle tip (`--standup`); a bounded replacement dispatch replaces
        # a spent serf in the SAME worktree (`--replace`), inheriting the
        # committed prior art. `court dispatch --standup` is the default shape
        # for fresh work, so the fresh path is just the verb + ids.
        if action.meta.get("fresh"):
            argv = ["dispatch", *action.quest_ids, "--standup"]
        else:
            argv = ["dispatch", *action.quest_ids, "--replace"]
    elif action.kind == "steward":
        # One bounded triage session on the trunk. The reason is forwarded so
        # the Steward's prompt names the exact stall the watch observed.
        argv = ["muster", "--why", action.reason or "board stale"]
    elif action.kind == "atelier":
        # Merge the batch into the ONE standing atelier (local git + server
        # ensure). `--standing` is the default; pass it explicitly so an engine
        # copy that predates the default flip still lands in the studio.
        argv = ["atelier", ",".join(action.quest_ids), "--standing"]
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
    # A dispatch the watch runs is the queue drain (vigil or replacement),
    # already capacity-gated — mark it so the dispatch guard does not refuse it.
    court_env = {"COURT_WATCH_DISPATCH": "1"} if argv and argv[0] == "dispatch" else None
    started = time.time()
    try:
        res = _court(repo, *argv,
                     timeout=COLLECT_TIMEOUT if action.kind == "collect" else timeout,
                     env=court_env)
        out = (res.stdout or "").strip()
        err = (res.stderr or "").strip()
        result = {
            "ok": res.returncode == 0,
            "exit": res.returncode,
            "argv": argv,
            "stdout_tail": out[-1200:],
            "stderr_tail": err[-1200:],
            "seconds": round(time.time() - started, 1),
            # Decided on the FULL stdout: the pack line is printed before the
            # (long) next-steps block, so a truncated tail can lose it.
            "packed": "Stamped" in out,
        }
        # `court rebase` exits 0 even when it reports a conflict, so the
        # reported conflict — not the exit code — is the failure signal. This
        # applies equally to the heuristic rebase action and a queued
        # `rebase:<QID>` vigil, which share the same CLI verb.
        if argv and argv[0] == "rebase" and result["ok"] and _merge_conflict_files(result):
            result["ok"] = False
        return result
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
    if "packed" in result:
        return bool(result.get("packed"))
    return "Stamped" in (result.get("stdout_tail") or "")


def _muster_session(result: dict) -> str:
    """Best-effort session id from the muster command's stdout."""
    m = re.search(r"session\s+([A-Za-z0-9_-]+)", result.get("stdout_tail") or "")
    return m.group(1) if m else ""


_STALE_CONVOY_RE = re.compile(r"Gatehouse worktree (\S+) already exists")
_LIVE_CONVOY_RE = re.compile(r"convoy (\S+) is LIVE")
# `court coin` / `court goad` abort the spawn when the deferred rebase hits a
# conflict (`_sync_base_before_spawn`) — a deterministic blocker that retrying
# on the short backoff can never clear, so the watch must instead escalate to
# a Steward who picks the resolution course. The capture runs to the sentence
# end so dotted paths (".court/quests/...md") survive.
_MERGE_CONFLICT_RE = re.compile(r"cannot spawn — `git merge \S+` conflicts in: (.*?)\. The merge")
# `court rebase <id>` reports conflicts as ``🔴 <id> CONFLICT in [a, b] — aborted``
# and exits 0 (a conflicted rebase is a reported outcome, not a CLI error), so
# execute() must both recognise the text and downgrade the result to a failure.
_REBASE_CONFLICT_RE = re.compile(r"CONFLICT in \[(.*?)\]")
# The standing-atelier single-writer guard refuses a merge while an artist turn
# is live (it prints this and merges nothing, exiting 0).
_ATELIER_ARTIST_LIVE_RE = re.compile(r"artist (?:session|turn) is live", re.IGNORECASE)


def _merge_conflict_files(result: dict) -> list:
    """Return the conflicting files a failed coin/goad/rebase named, or [].

    ``ERROR: coin: cannot spawn — `git merge castle` conflicts in:
    .court/quests/Q1046-....md, apps/foo/bar.py. The merge was aborted
    cleanly; ...`` — the spawn probe aborts deterministically until a human
    or Steward resolves the rebase, so the watch must not treat this as a
    transient failure to retry on the short backoff."""
    text = (result.get("stderr_tail") or "") + "\n" + (result.get("stdout_tail") or "")
    match = _MERGE_CONFLICT_RE.search(text)
    if match:
        return [f.strip() for f in match.group(1).split(",") if f.strip()]
    match = _REBASE_CONFLICT_RE.search(text)
    if match:
        return [f.strip() for f in match.group(1).split(",") if f.strip()]
    return []


def _archived_quest_ids(repo: Path) -> set:
    """Quest ids filed in `.court/archive/` (moved out of the active ledger).
    Cheap: filenames are `{quest.id}.md`, so the stem IS the id."""
    try:
        from . import store
        archive_dir = store.get_archive_dir(Path(repo) / ".court")
        if not archive_dir.is_dir():
            return set()
        return {p.stem for p in archive_dir.glob("Q*.md")}
    except Exception:  # noqa: BLE001 — observation must never kill a tick
        return set()


def stale_conflict_ids(repo: Path, status_by_id: dict, razed_ids=None, tracked_ids=None) -> set:
    """Quest ids whose `merge_conflict` markers are stale and must be dropped.

    A marker is stale when the Quest is terminal (DONE/READY_TO_RAZE), already
    razed, filed in the archive, OR no longer present in the active ledger at
    all. The last case is the 2026-11-15 defect: 27 of 31 tracked conflict
    Quests were not in `.court/quests/` any more, so the digest listed only dead
    Quests while the live blockers were buried in noise. `tracked_ids` is the
    set of Quests the watch's state still carries markers for; any id absent
    from `status_by_id` (the active ledger view) is stale. Only pass
    `tracked_ids` when `status_by_id` is an AUTHORITATIVE active-ledger view."""
    active_status = status_by_id or {}
    stale = {
        qid for qid, status in active_status.items()
        if str(status or "").strip().upper() in CONFLICT_TERMINAL_STATUSES
    }
    stale |= {qid for qid in (razed_ids or []) if qid}
    stale |= _archived_quest_ids(repo)
    if tracked_ids is not None:
        stale |= {qid for qid in tracked_ids if qid and qid not in active_status}
    return stale


def prune_terminal_conflicts(state: dict, stale_ids) -> int:
    """Drop `merge_conflict` markers (and their escalation state) for terminal/
    razed/archived Quests. Returns how many markers were cleared.

    The marker is only ever cleared by a later successful rebase/coin, so once a
    Quest reaches a terminal state its old record persists indefinitely. Pruning
    at tick time (not only at the digest) keeps the state file honest and stops
    the marker re-surfacing from an older state file on a later tick."""
    stale = {qid for qid in (stale_ids or []) if qid}
    if not stale:
        return 0
    cleared = 0
    qbook = state.get("quests") or {}
    for qid in stale:
        st = qbook.get(qid)
        if isinstance(st, dict) and isinstance(st.get("merge_conflict"), dict):
            st.pop("merge_conflict", None)  # carries escalated_ts with it
            cleared += 1
    # Escalation bookkeeping lives under `notified`, keyed by qid OR a
    # comma-joined cohort id, so prune entries whose ids are ALL stale.
    notified = state.get("notified")
    if isinstance(notified, dict):
        for key in list(notified):
            entry = notified.get(key)
            if not isinstance(entry, dict):
                continue
            ids = [p for p in str(key).split(",") if p]
            if not ids or not all(pid in stale for pid in ids):
                continue
            for nk in [k for k in entry if str(k).startswith("merge_conflict")]:
                entry.pop(nk, None)
            if not entry:
                notified.pop(key, None)
    return cleared


def _stale_convoy_worktree(result: dict) -> str:
    """Return the stale gatehouse worktree path a failed collect named, or "".

    `court collect` stamps the convoy and THEN refuses to re-create a gatehouse
    worktree left behind by a convoy that died mid-integration:
    ``ERROR: Gatehouse worktree <path> already exists (...). Refusing to
    re-create it``. That is a distinct, human-fixable condition — not a
    transient failure to retry on the short backoff."""
    text = (result.get("stderr_tail") or "") + "\n" + (result.get("stdout_tail") or "")
    match = _STALE_CONVOY_RE.search(text)
    return match.group(1) if match else ""


def _live_convoy_name(result: dict) -> str:
    """Return the convoy a failed collect refused to disturb, or "".

    ``court collect`` refuses a new convoy while another one is mid-integration:
    ``ERROR: convoy cogship-396 is LIVE — its gatehouse worktree ...``. That is
    the system working, not a Quest failure — a new convoy cannot be packed
    until the live one promotes. Retrying on the short backoff just burns the
    per-tick action budget and inflates per-Quest failure counters (the
    2026-11-01 cogship-396 retry storm: five attempts in sixteen minutes), so
    the convoy waits out the gate window instead."""
    text = (result.get("stderr_tail") or "") + "\n" + (result.get("stdout_tail") or "")
    match = _LIVE_CONVOY_RE.search(text)
    return match.group(1) if match else ""



def _atelier_artist_live(result: dict) -> bool:
    """Whether `court atelier` stood down because an artist turn is live.

    The single-writer guard inside the standing-atelier merge prints this to
    stderr and exits 0 (it merged nothing). That is not a failure — the merge
    waits for the artist to go idle — so the watch must hold, not count it."""
    text = (result.get("stderr_tail") or "") + "\n" + (result.get("stdout_tail") or "")
    return bool(_ATELIER_ARTIST_LIVE_RE.search(text)) or "not merging new Quests" in text


def _heal_stale_convoys(state: dict) -> None:
    """Clear a stale-convoy hold once its gatehouse worktree is gone (the
    operator tore it down), so the next tick can re-collect immediately instead
    of waiting out the long stale backoff."""
    for convoy in (state.get("convoys") or {}).values():
        if not isinstance(convoy, dict):
            continue
        stale = convoy.get("stale")
        if not stale:
            continue
        wt = (stale or {}).get("worktree") if isinstance(stale, dict) else ""
        if not wt or not Path(wt).is_dir():
            convoy.pop("stale", None)
            convoy["retry_at"] = 0


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
    live_convoy = "" if ok else _live_convoy_name(result)
    qstate = state.setdefault("quests", {})

    if action.kind == "vigil":
        if repo is not None:
            vid = action.meta.get("vigil_id")
            for spec in vigils.load_all(repo, include_finished=True):
                if spec.get("id") == vid and spec.get("status") == "active":
                    vigils.mark_fired(repo, spec, result)
                    break
        # A queued action that dies on `git merge castle` is the SAME
        # deterministic blocker the heuristic paths already stamp: record it
        # so a chained coin/goad holds for a Steward instead of firing next
        # tick into the identical conflict and burning its own queue entry.
        mc = _merge_conflict_files(result)
        if not ok and mc:
            verb = _vigil_verb({"then": action.meta.get("then") or ""})
            if verb in ("rebase", "goad", "coin", "dispatch"):
                for qid in action.quest_ids:
                    st = qstate.setdefault(qid, {})
                    prev = st.get("merge_conflict") or {}
                    st["merge_conflict"] = {
                        "files": mc, "ts": now,
                        "escalated_ts": prev.get("escalated_ts"),
                    }
        return

    if action.kind == "goad":
        for qid in action.quest_ids:
            st = qstate.setdefault(qid, {})
            mc = _merge_conflict_files(result)
            # A rebase conflict is a deterministic blocker, not a dead session:
            # record it so plan() holds the quest and escalates to a Steward
            # instead of burning the goad budget retrying. The goad counter is
            # a per-attempt budget, so a conflict does NOT count as an attempt.
            if ok:
                st["goads"] = int(action.meta.get("goads_before") or 0) + 1
                st["last_goad_ts"] = now
                st.pop("merge_conflict", None)
            elif mc:
                prev = st.get("merge_conflict") or {}
                st["merge_conflict"] = {
                    "files": mc, "ts": now,
                    "escalated_ts": prev.get("escalated_ts"),
                }
            else:
                st["goads"] = int(action.meta.get("goads_before") or 0) + 1
    elif action.kind == "dispatch":
        # A replacement dispatch advances to the next serf slot; it spends no
        # goad, and clears the goad cooldown so the new serf can be prompted.
        # A FRESH dispatch (the charter pickup lane) has no prior serf to
        # advance: success moves the Quest to WORKING upstream so it never
        # re-triggers; a failure backs the Quest off so the watch does not
        # re-dispatch it on the very next tick.
        for qid in action.quest_ids:
            st = qstate.setdefault(qid, {})
            if ok and not action.meta.get("fresh"):
                st["serf_index"] = int(action.meta.get("serf_index") or 0) + 1
                st["last_goad_ts"] = 0
            elif not ok and action.meta.get("fresh") and backoff:
                st["retry_at"] = now + backoff
    elif action.kind == "coin":
        for qid in action.quest_ids:
            st = qstate.setdefault(qid, {})
            mc = _merge_conflict_files(result)
            # Same class as the goad branch above: a `git merge castle`
            # conflict blocks the spawn deterministically. Hold the quest for
            # a Steward decision — do not burn the coin ceiling on a blocker.
            if ok:
                st["coins"] = int(st.get("coins") or 0) + 1
                st["last_coin_ts"] = now
                st.pop("merge_conflict", None)
            elif mc:
                prev = st.get("merge_conflict") or {}
                st["merge_conflict"] = {
                    "files": mc, "ts": now,
                    "escalated_ts": prev.get("escalated_ts"),
                }
            else:
                st["coins"] = int(st.get("coins") or 0) + 1
    elif action.kind == "rebase":
        # Drift is not a worker attempt: a successful rebase clears any hold,
        # stamps the rebase cooldown, and the Quest coins on the NEXT tick. A
        # conflict is recorded exactly like a blocked spawn so plan() holds the
        # Quest for a Steward. Never touch coins/goads — this verb spends none.
        for qid in action.quest_ids:
            st = qstate.setdefault(qid, {})
            mc = _merge_conflict_files(result)
            if ok:
                st["last_rebase_ts"] = now
                st.pop("merge_conflict", None)
                st["retry_at"] = 0
            elif mc:
                prev = st.get("merge_conflict") or {}
                st["merge_conflict"] = {
                    "files": mc, "ts": now,
                    "escalated_ts": prev.get("escalated_ts"),
                }
            elif backoff:
                st["retry_at"] = now + backoff
    elif action.kind == "prune":
        # Session-store housekeeping on its own state key (board-level, not a
        # Quest). The interval only advances on a full success so a refused or
        # lock-contended vacuum is retried on the next idle window instead of
        # being skipped for a whole day.
        maint = state.setdefault("maintenance", {})
        if ok:
            maint["session_prune_ts"] = now
            maint["session_prune_fails"] = 0
            maint["session_prune_error"] = ""
            maint["session_prune_report"] = (result.get("stdout_tail") or "")[-600:]
        else:
            maint["session_prune_fails"] = int(maint.get("session_prune_fails") or 0) + 1
            maint["session_prune_error"] = (result.get("stderr_tail") or "")[-400:]
            if backoff:
                maint["session_prune_retry_at"] = now + backoff
    elif action.kind == "pgprune":
        maint = state.setdefault("maintenance", {})
        if ok:
            maint["pg_prune_ts"] = now
            maint["pg_prune_error"] = ""
            maint["pg_prune_report"] = (result.get("stdout_tail") or "")[-600:]
        else:
            maint["pg_prune_fails"] = int(maint.get("pg_prune_fails") or 0) + 1
            maint["pg_prune_error"] = (
                result.get("stderr_tail") or result.get("stdout_tail") or ""
            )[-400:]
            if backoff:
                maint["pg_prune_retry_at"] = now + backoff
    elif action.kind == "raze":
        # A successful raze tears the worktree down and archives the Quest, so
        # it simply stops being planned. A refusal (dirty tree, a branch that
        # turns out not to be an ancestor) is retried on the short backoff.
        for qid in action.quest_ids:
            st = qstate.setdefault(qid, {})
            st["last_raze_ts"] = now
            if not ok:
                st["raze_fails"] = int(st.get("raze_fails") or 0) + 1
                if backoff:
                    st["raze_retry_at"] = now + backoff
            else:
                st.pop("raze_fails", None)
                st.pop("raze_retry_at", None)
    elif action.kind == "charter":
        req_state = state.setdefault("requests", {})
        for rid in action.quest_ids:
            st = req_state.setdefault(rid, {})
            st["last_attempt_ts"] = now
            if ok:
                st["fails"] = 0
                st.pop("error", None)
            else:
                st["fails"] = int(st.get("fails") or 0) + 1
                st["error"] = (result.get("stderr_tail") or result.get("stdout_tail") or "")[-400:]
                if backoff:
                    st["retry_at"] = now + backoff
    elif action.kind == "regate":
        # A regate hands a squashed/diverged Quest back to a fresh convoy. Its
        # success is observable on the next tick (the Quest leaves
        # READY_TO_RAZE); a repeatable failure counts toward `max_regates`,
        # after which the Quest is escalated to a Steward instead of looping.
        for qid in action.quest_ids:
            st = qstate.setdefault(qid, {})
            st["last_regate_ts"] = now
            if ok:
                st["regate_fails"] = 0
            else:
                st["regate_fails"] = int(st.get("regate_fails") or 0) + 1
                if backoff:
                    st["regate_retry_at"] = now + backoff
    elif action.kind == "teardown":
        # A successful teardown clears the stale-convoy hold so collect can
        # re-pack the convoy on the next tick. A failed one is remembered and
        # retried on the short backoff, then escalated to a human rather than
        # looping forever (see plan's max_teardown_fails gate).
        key = action.meta.get("convoy") or "new"
        convoy = state.setdefault("convoys", {}).setdefault(key, {})
        if ok:
            convoy.pop("stale", None)
            convoy.pop("teardown_fails", None)
            convoy.pop("teardown_error", None)
            convoy.pop("teardown_retry_at", None)
            convoy["retry_at"] = 0
        else:
            convoy["teardown_fails"] = int(convoy.get("teardown_fails") or 0) + 1
            convoy["teardown_error"] = (
                result.get("stderr_tail") or result.get("stdout_tail") or ""
            )[-400:]
            if backoff:
                convoy["teardown_retry_at"] = now + backoff
    elif action.kind == "steward":
        # Muster bookkeeping lives on its own state key: it is a board-level
        # action, not a Quest action. A failed muster retries on the short
        # backoff but never earns the full cooldown; a successful one starts
        # the cooldown and counts against the rolling 24-hour cap.
        st = state.setdefault("steward", {})
        st["spawns"] = [float(t) for t in st.get("spawns", []) if (now - float(t)) < 86400]
        st["fails"] = int(st.get("fails") or 0) + 1
        st["last_error"] = (result.get("stderr_tail") or result.get("stdout_tail") or "")[-400:]
        if ok:
            # Stamp every merge-conflict quest the muster carried so the same
            # conflict is escalated once, not re-mustered on every cooldown.
            # A FAILED muster (lease held, kilo missing) leaves the markers
            # untouched so the same conflict remains eligible on the retry.
            if action.meta.get("merge_conflict"):
                for qid in action.quest_ids:
                    qst = qstate.setdefault(qid, {})
                    mc = qst.get("merge_conflict")
                    if isinstance(mc, dict):
                        mc["escalated_ts"] = now
            st["fails"] = 0
            st["last_error"] = ""
            st["last_ts"] = now
            st["fails"] = 0
            st["last_error"] = ""
            st["last_ts"] = now
            st["retry_at"] = 0
            st["spawns"].append(now)
            st["last_session"] = _muster_session(result)
        elif backoff:
            st["retry_at"] = now + backoff
    elif action.kind == "collect":
        key = action.meta.get("convoy") or action.cogship or "new"
        convoy = state.setdefault("convoys", {}).setdefault(key, {})
        stale_wt = "" if ok else _stale_convoy_worktree(result)
        if ok and _collect_packed(result):
            convoy["last_ts"] = now
            convoy["retry_at"] = 0
            convoy["fails"] = 0
            convoy.pop("stale", None)
        elif stale_wt:
            # The convoy stamped but could not re-create its gatehouse worktree:
            # a prior convoy died mid-integration. This is a human-fixable
            # condition, NOT a transient failure — hold the convoy (escalated
            # once by plan) instead of re-stamping/retrying every few minutes.
            convoy["stale"] = {
                "ts": now,
                "worktree": stale_wt,
                "reason": "gatehouse worktree already exists (prior convoy died mid-integration)",
            }
            convoy["fails"] = int(convoy.get("fails") or 0) + 1
            convoy["last_error"] = "gatehouse worktree already exists"
            stale_backoff = float(action.meta.get("stale_backoff") or DEFAULT_STALE_CONVOY_BACKOFF)
            convoy["retry_at"] = now + stale_backoff
        elif ok:
            # exit 0 but nothing packed (PENDING UI review, dirty tree, charter
            # violations) is NOT progress. Back the convoy off hard so a blocked
            # convoy cannot spend the per-tick action budget every tick and
            # starve every other quest (the cogship-382 starvation).
            noop = float(action.meta.get("noop_backoff") or DEFAULT_COLLECT_NOOP_BACKOFF)
            convoy["retry_at"] = now + noop
        elif live_convoy:
            # Another convoy owns the gatehouse right now. This is a busy
            # signal, not a failure: wait out the gate window and leave the
            # Quest counters untouched so a healthy convoy cannot look sick.
            live_backoff = float(action.meta.get("live_backoff") or DEFAULT_LIVE_CONVOY_BACKOFF)
            convoy["live"] = {"ts": now, "convoy": live_convoy}
            convoy["last_error"] = f"convoy {live_convoy} is LIVE"
            convoy["retry_at"] = now + live_backoff
        elif backoff:
            # A hard failure (argparse slip, busy lock) retries quickly at first,
            # then backs off exponentially so a persistent blocker costs a
            # window, not a 5-minute loop forever (cogship-381/399 storm).
            convoy["fails"] = int(convoy.get("fails") or 0) + 1
            convoy["last_error"] = (result.get("stderr_tail") or result.get("stdout_tail") or "")[-300:]
            convoy["retry_at"] = now + _failure_backoff(
                backoff, convoy["fails"], action.meta.get("max_retry_backoff"))
    elif action.kind == "atelier":
        # A local merge into the standing atelier. Success stamps the per-Quest
        # cooldown (manifest membership also excludes it next tick, so this is a
        # backstop). A single-writer refusal — an artist turn is live — merged
        # nothing, so it holds on the short backoff without counting a failure.
        # A hard failure backs off exponentially.
        blocked = _atelier_artist_live(result)
        for qid in action.quest_ids:
            st = qstate.setdefault(qid, {})
            st["last_atelier_ts"] = now
            if ok and not blocked:
                st.pop("atelier_retry_at", None)
                st.pop("atelier_fails", None)
            elif blocked:
                st["atelier_retry_at"] = now + float(action.meta.get("retry_backoff") or 0)
            else:
                st["atelier_fails"] = int(st.get("atelier_fails") or 0) + 1
                st["atelier_retry_at"] = now + _failure_backoff(
                    float(action.meta.get("retry_backoff") or 0),
                    st["atelier_fails"], action.meta.get("max_retry_backoff"))
    elif action.kind == "cleargate":
        # The dead stamp is gone on success: drop the hold's first-seen stamp so
        # the Quest plans normally (and re-packs) on the next tick. A failure
        # backs off and retries rather than spinning.
        for qid in action.quest_ids:
            st = qstate.setdefault(qid, {})
            if ok:
                st.pop("stale_gatekeeper_since", None)
                st["last_cleargate_ts"] = now
            else:
                st["cleargate_fails"] = int(st.get("cleargate_fails") or 0) + 1
                if backoff:
                    st["retry_at"] = now + backoff

    for qid in action.quest_ids:
        st = qstate.setdefault(qid, {})
        st["last_attempt_ts"] = now
        if ok:
            st["fails"] = 0
            st["last_error"] = ""
            st["last_kind"] = action.kind
            st["retry_at"] = 0
        elif live_convoy:
            st["last_error"] = f"convoy {live_convoy} is LIVE — collect deferred"
        elif isinstance(st.get("merge_conflict"), dict):
            # A held rebase conflict: do not inflate the fails counter the way
            # a transient failure would — the quest is not broken, it is parked
            # for a Steward. Keep a clear error and the probe cadence on
            # `retry_at` so the marker/hold logic governs, not the fail count.
            st["last_error"] = (result.get("stderr_tail") or result.get("stdout_tail") or "")[-400:]
            if backoff:
                st["retry_at"] = now + backoff
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


def fold_ledger(repo: Path, say=print) -> bool:
    """Fold branch-side quest paperwork onto the trunk before snapshotting.

    A worker self-advances (`court advance`) on its own branch; the board and
    the planner read the trunk ledger. Without this fold a finished Tribute
    still reads `WORKING`, so the watch treats it as a dead worker and goads a
    completed Quest instead of sending a Master of Coin. Deterministic and
    fail-soft: a missing branch or an edit war is reported, never fatal.
    """
    repo = Path(repo)
    if not (repo / ".court" / "LEDGER.md").exists():
        return False
    try:
        if subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "castle"],
            cwd=str(repo), capture_output=True, timeout=30,
        ).returncode != 0:
            return False
        res = subprocess.run(
            [sys.executable, "-m", "court.cli", "sync", "--all"],
            cwd=str(repo), capture_output=True, text=True, timeout=180,
        )
    except Exception as e:  # noqa: BLE001 — observation/mirroring must never kill a tick
        say(f"⚠️  ledger sync failed: {e}")
        return False
    if res.returncode != 0:
        say(f"⚠️  ledger sync reported a problem: {(res.stderr or res.stdout or '').strip()[:200]}")
        return False
    return True


def _current_branch(repo: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, timeout=20,
        )
        return res.stdout.strip() if res.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _maybe_remediate_pickup(repo: Path, state: dict, policy: "Policy",
                            dry_run: bool, say=print) -> Optional[dict]:
    """Clear a repeatable dirty-tree pickup block mechanically, then retry once.

    The pickup gate exists so an atomic paperwork commit never mixes with a
    human's uncommitted edit. It does not need a reasoning worker: when the
    same rejection repeats and every blocking path is scratch under a known
    root (or a regenerable report), the watch quarantines/restores it itself.
    Anything else is left exactly where it is for a human.
    """
    if not policy.pickup_remedy:
        return None
    streak = state.get("pickup_streak") or {}
    if int(streak.get("count") or 0) < policy.pickup_remedy_after:
        return None
    cls = streak.get("class")
    paths = sorted({str(p) for p in (streak.get("paths") or []) if str(p)})
    previous = state.get("pickup_remedy") or {}
    same = (previous.get("class") == cls
            and sorted(str(p) for p in (previous.get("paths") or [])) == paths)
    if same and _within(previous.get("ts"), policy.pickup_remedy_cooldown, time.time()):
        return None
    try:
        from . import pickup as pickup_lane
        verdict = pickup_lane.remediate(repo, cls, paths, dry_run=dry_run, say=say)
    except Exception as e:  # noqa: BLE001 — a remedy must never kill a tick
        verdict = {"ok": False, "action": None, "error": str(e)[:300],
                   "moved": [], "restored": []}
    record = {
        "ts": _now_iso(),
        "event": "pickup_remedy",
        "rejection_class": cls,
        "rejection_paths": paths,
        "action": verdict.get("action"),
        "ok": bool(verdict.get("ok")),
        "moved": list(verdict.get("moved") or []),
        "restored": list(verdict.get("restored") or []),
        "quarantine_dir": verdict.get("quarantine_dir") or "",
        "error": verdict.get("error"),
        "dry_run": dry_run,
    }
    _journal(repo, record)
    state["pickup_remedy"] = {"ts": time.time(), "class": cls, "paths": paths,
                              "ok": record["ok"], "action": record["action"],
                              "error": record["error"]}
    if not record["ok"]:
        say(f"⚠️  pickup stays blocked — {record['error'] or 'no mechanical remedy'}")
        return record
    if dry_run:
        say(f"[dry-run] would remediate {cls} on {len(paths)} path(s) "
            f"({record['action']}) and retry the pickup")
        return record
    say(f"🧹 pickup remediation ({record['action']}): "
        f"{len(record['moved'])} quarantined, {len(record['restored'])} restored — "
        f"retrying the pickup")
    _run_pickup(repo, state, dry_run=False, say=say)
    return record


def _run_pickup(repo: Path, state: dict, dry_run: bool, say=print) -> dict:
    """Apply remote Pleasance paperwork at the top of a tick.

    The pickup lane commits onto the checked-out trunk, so it is only safe on
    `castle`; a watch accidentally started on `main` (or a quest branch) must
    never splice paperwork into that branch. A no-op (no pleasance branch) is
    normal. The result is summarized into state so the pulse can surface a
    rejection without re-fetching.

    A rejection that repeats identically tick after tick is a STREAK: the same
    class + the same paths. The journal records the start and the milestones
    (3/10/25/100) rather than each tick, so a wedged lane reads as one growing
    number instead of 26 indistinguishable lines; from the third repeat the
    rejection also carries a mechanical remedy.
    """
    branch = _current_branch(repo)
    if branch != "castle":
        result = {"skipped": f"not on castle (on {branch or 'detached'})"}
    else:
        try:
            from . import pickup as pickup_lane
            result = pickup_lane.run(repo, fetch=True, base="castle",
                                     dry_run=dry_run, say=say)
        except Exception as e:  # noqa: BLE001 — pickups must never kill a tick
            result = {"picked": [], "rejected": f"pickup error: {e}",
                      "rejection_class": "unknown", "rejection_paths": []}

    ts = _now_iso()
    rejected = result.get("rejected")
    streak = None
    if rejected:
        cls = result.get("rejection_class") or "unknown"
        paths = sorted({str(p) for p in (result.get("rejection_paths") or []) if str(p)})
        prev = state.get("pickup_streak") or {}
        same = (prev.get("class") == cls
                and sorted(str(p) for p in (prev.get("paths") or [])) == paths)
        count = int(prev.get("count") or 0) + 1 if same else 1
        since = prev.get("since") if same and prev.get("since") else ts
        streak = {"count": count, "since": since, "class": cls,
                  "paths": paths, "last_ts": ts}
        state["pickup_streak"] = streak
    else:
        # A successful pickup, a no-op, or an off-trunk skip ends the streak.
        state.pop("pickup_streak", None)

    state["last_pickup"] = {
        "ts": ts,
        "picked": list(result.get("picked") or []),
        "rejected": rejected,
        "skipped": result.get("skipped"),
        # Per-file supersession (an unusable decree, a frozen charter) is
        # journaled, not fatal; the pulse names it so an away steward sees
        # exactly which paperwork was held back and why.
        "skipped_files": [
            {"path": str(i.get("path")), "reason": str(i.get("reason"))[:300]}
            for i in (result.get("skipped_files") or [])
        ],
        "rejection_class": (streak or {}).get("class"),
        "rejection_paths": list((streak or {}).get("paths") or []),
    }
    if streak:
        state["last_pickup"]["streak_count"] = streak["count"]
        state["last_pickup"]["streak_since"] = streak["since"]
        if streak["count"] >= 3:
            try:
                from . import pickup as pickup_lane
                remedy = pickup_lane.pickup_remedy(streak["class"], streak["paths"])
            except Exception:  # noqa: BLE001 — telemetry must never kill a tick
                remedy = ""
            if remedy:
                state["last_pickup"]["remedy"] = remedy

    # Journal at streak start / class change (count == 1) and at milestones;
    # a successful pickup is always worth one line.
    if streak:
        if streak["count"] == 1 or streak["count"] in PICKUP_STREAK_MILESTONES:
            _journal(repo, {
                "ts": ts,
                "event": "pickup",
                "picked": state["last_pickup"]["picked"],
                "rejected": rejected,
                "rejection_class": streak["class"],
                "rejection_paths": streak["paths"],
                "pickup_streak": streak["count"],
                "streak_since": streak["since"],
                "remedy": state["last_pickup"].get("remedy"),
            })
    elif result.get("picked"):
        _journal(repo, {
            "ts": ts,
            "event": "pickup",
            "picked": state["last_pickup"]["picked"],
            "rejected": None,
            "rejection_class": None,
            "rejection_paths": [],
        })
    return result


def _write_pulse(repo: Path, policy: "Policy", snap: dict, state: dict,
                 workers: dict, notes: list, dry_run: bool, say=print) -> None:
    """Write the compact steward pulse every tick (and optionally publish it)."""
    try:
        from . import pulse
        payload = pulse.build(repo, snap=snap, workers=workers, state=state,
                              notes=notes)
        markdown = pulse.render_markdown(payload)
        payload_json = json.dumps(payload, indent=2) + "\n"
        pulse.write_local(repo, markdown, payload_json)
        if policy.publish_pulse and not dry_run:
            result = pulse.publish(repo, markdown, payload_json,
                                   branch=policy.pulse_branch)
            if result.get("published"):
                say(f"📡 pulse published to {policy.pulse_branch} at {result['commit'][:12]}")
            elif result.get("reason") not in ("unchanged", None):
                say(f"⚠️  pulse publish failed: {result.get('reason')}")
    except Exception as e:  # noqa: BLE001 — telemetry must never kill a tick
        _journal(repo, {"ts": _now_iso(), "event": "pulse_failed", "error": str(e)[:500]})


def tick(repo: Path, policy: Policy, state: dict, dry_run: bool = False, say=print) -> dict:
    now = time.time()
    # Fresh branch-side self-advances first: the snapshot below is the board
    # the planner reasons over, and it reads the trunk ledger.
    if not dry_run:
        state.setdefault("supervisor_started", now)
        fold_ledger(repo, say=say)
        if policy.pickup:
            _run_pickup(repo, state, dry_run, say=say)
            _maybe_remediate_pickup(repo, state, policy, dry_run, say=say)
    spawns = _prune_spawns(state, now)
    _heal_stale_convoys(state)
    workers = live_kilo_workers(repo)
    # A Steward triage session runs on the trunk (repo root), where the
    # worktree scan cannot see it. Surface its lease so `plan` never musters a
    # second Steward onto a board that is already being looked at.
    try:
        workers["steward_lease"] = role_state(str(repo), "steward")
    except Exception:  # noqa: BLE001 — observation must never kill a tick
        workers["steward_lease"] = {"state": "idle"}
    try:
        snap = snapshot(repo, timeout=policy.status_timeout)
    except Exception as e:  # noqa: BLE001
        say(f"⚠️  snapshot failed: {e}")
        _journal(repo, {"ts": _now_iso(), "event": "snapshot_failed", "error": str(e)[:500]})
        return {"actions": [], "notes": [], "workers": workers, "error": str(e)}

    try:
        now_dt = datetime.now(timezone.utc)
        qmap = _quest_map(snap)
        ready, vigil_notes = vigils.ready_vigils(repo, qmap, now=now_dt)
        charter_ready = _charter_ready(repo, qmap, now=now_dt)
    except Exception as e:  # noqa: BLE001
        ready, vigil_notes, charter_ready = [], [], []
        say(f"⚠️  vigil evaluation failed: {e}")
    # GATE age, for convoy coalescing. `plan` is pure, so the watch records when
    # it first saw each Quest in GATE: a cohort waits for same-app teammates but
    # never longer than `collect_batch_window`.
    qbook = state.setdefault("quests", {})
    for q in snap.get("quests", []):
        if q.get("kind") == "epic":
            continue
        st = qbook.setdefault(q["id"], {})
        if (q.get("status") or "").upper() == "GATE":
            st.setdefault("gate_since_ts", now)
        else:
            st.pop("gate_since_ts", None)
    # Prune stale merge-conflict markers for terminal/razed/archived Quests
    # BEFORE planning, so a marker left over from a terminal Quest can never be
    # held, re-escalated, or counted as a blocker this tick.
    try:
        _status_by_id = {
            q["id"]: q.get("status") for q in snap.get("quests", [])
            if q.get("kind") != "epic"
        }
        _razed_ids = {
            q["id"] for q in snap.get("quests", [])
            if q.get("kind") != "epic" and q.get("razed_at")
        }
        _cleared = prune_terminal_conflicts(
            state,
            stale_conflict_ids(repo, _status_by_id, _razed_ids,
                               tracked_ids=set(qbook.keys())))
        if _cleared:
            say(f"🧹 cleared {_cleared} stale merge-conflict marker(s) for terminal/razed Quests")
    except Exception as e:  # noqa: BLE001 — pruning must never kill a tick
        say(f"⚠️  conflict-marker prune failed: {e}")
    vigil_notice: list[dict] = []
    for vn in vigil_notes:
        vigil_notice.append({"quest": vn["id"], "level": "vigil", "text": vn["text"]})
    # A GATE Quest carrying an unmet UI mandate must not be packed: compute the
    # set here (plan is pure) so the planner can hold it and escalate.
    ui_mandate_ids: list[str] = []
    try:
        from . import convoy_guard
        for q in snap.get("quests", []):
            if q.get("kind") == "epic":
                continue
            if (q.get("status") or "").upper() not in ("GATE", "READY_TO_RAZE"):
                continue
            outstanding, _detail = convoy_guard.ui_mandate_outstanding(repo, q["id"])
            if outstanding:
                ui_mandate_ids.append(q["id"])
    except Exception as e:  # noqa: BLE001 — a charter parse must not kill a tick
        say(f"⚠️  UI mandate scan failed: {e}")
    # UI-review-pending GATE Quests: not collectable, so route them to the
    # standing atelier. The snapshot already carries each Quest's `ui_review`;
    # manifest membership (already merged) and a live artist turn decide whether
    # the merge can run this tick.
    ui_pending_ids: list[str] = []
    atelier_ready: list[str] = []
    atelier_blocked = False
    try:
        from . import atelier as _atelier
        atelier_members = set(_atelier.members(repo))
        atelier_wt = str(_atelier.worktree_path(repo))
        if Path(atelier_wt).is_dir():
            try:
                _dirs = workers.get("dirs") or {}
                atelier_blocked = bool(occupied(atelier_wt)) or bool(
                    _dirs.get(str(Path(atelier_wt).resolve())))
            except Exception:  # noqa: BLE001 — observation must never kill a tick
                atelier_blocked = False
        for q in snap.get("quests", []):
            if q.get("kind") == "epic":
                continue
            if (q.get("status") or "").upper() != "GATE":
                continue
            if q.get("pending_audience"):
                continue
            if not (q.get("ui_review") or "").upper().startswith(("PENDING", "DEFERRED")):
                continue
            qid = q["id"]
            ui_pending_ids.append(qid)
            if qid in atelier_members or qid in ui_mandate_ids:
                continue
            atelier_ready.append(qid)
    except Exception as e:  # noqa: BLE001 — the atelier scan must not kill a tick
        say(f"⚠️  atelier scan failed: {e}")
    try:
        raze_waiting = [q["id"] for q in snap.get("quests", [])
                        if q.get("kind") != "epic"
                        and (q.get("status") or "").upper() == "READY_TO_RAZE"
                        and not q.get("razed_at")]
        _raze_ready, raze_regate = _bucket_raze(repo, raze_waiting)
    except Exception as e:  # noqa: BLE001 — bucketing must never kill a tick
        raze_regate = []
        say(f"⚠️  raze bucketing failed: {e}")
    try:
        from . import requests as requests_mod
        open_requests = requests_mod.load_open(repo)
    except Exception as e:  # noqa: BLE001 — a bad packet must not kill a tick
        open_requests = []
        say(f"⚠️  charter request scan failed: {e}")
    # Resolve which convoys have already been promoted (from the convoy record
    # and, for the candidates in play, the trunk) so the re-ship decision keys
    # on the CONVOY, not the inconsistently-written per-Quest field.
    promoted_cogships: set = set()
    try:
        gate_candidates = {
            _norm_cogship(q.get("cogship_id") or "")
            for q in snap.get("quests", [])
            if q.get("kind") != "epic"
            and (q.get("status") or "").upper() == "GATE"
            and (q.get("gatekeeper_session_id") or "").strip()
            and (q.get("cogship_id") or "").strip()
        }
        gate_candidates.discard("")
        promoted_cogships = _promoted_cogships(repo, candidates=gate_candidates)
    except Exception as e:  # noqa: BLE001 — promotion scan must never kill a tick
        promoted_cogships = set()
        say(f"⚠️  convoy promotion scan failed: {e}")
    actions, notes = plan(snap, state, policy, workers, now=now, ready_vigils=ready,
                          charter_ready=charter_ready, raze_regate=raze_regate,
                          charter_requests=open_requests, ui_mandate_ids=ui_mandate_ids,
                          atelier_ready=atelier_ready, atelier_blocked=atelier_blocked,
                          ui_pending_ids=ui_pending_ids, promoted_cogships=promoted_cogships)
    notes = vigil_notice + notes
    _record_escalations(state, notes, now)
    # Maintain the stale-Gatekeeper hold's first-seen stamp: set on the first
    # tick the condition is observed and dropped the moment it stops, so plan()
    # can expire a dead session stamp instead of parking the Quest forever.
    try:
        held = {n["quest"] for n in notes
                if n.get("key") in ("stale_gatekeeper", "stale_gatekeeper_cleared")}
        live_ids = {q["id"] for q in snap.get("quests", []) if q.get("kind") != "epic"}
        qbook_all = state.setdefault("quests", {})
        for qid in held:
            if qid in live_ids:
                qbook_all.setdefault(qid, {}).setdefault("stale_gatekeeper_since", now)
        for qid, st in qbook_all.items():
            if isinstance(st, dict) and "stale_gatekeeper_since" in st and qid not in held:
                st.pop("stale_gatekeeper_since", None)
    except Exception as e:  # noqa: BLE001 — bookkeeping must never kill a tick
        say(f"⚠️  stale-Gatekeeper hold update failed: {e}")
    # Board fingerprint: only re-stamp the "observed since" timestamp when the
    # board actually changes. This is what makes "stale for N minutes" honest
    # across daemon restarts (state lives outside the repo).
    try:
        sig = board_signature(_quest_map(snap), notes)
    except Exception:  # noqa: BLE001
        sig = ""
    if sig and state.get("board_signature") != sig:
        state["board_signature"] = sig
        state["board_sig_since"] = now
    executed = []
    for action in actions:
        _spends = _action_spawns(action)
        if _spends and len(spawns) >= policy.max_spawns_per_hour:
            # plan() already bounds its spawn actions to the hour's remaining
            # slots, so this is a safety net for a budget spent between planning
            # and execution (a concurrent tick, a failed spawn that still
            # charged). It skips only the spawning action: a local `rebase` /
            # `teardown` / `advance` behind it costs no budget and must still run.
            say(f"\u23f8\ufe0f  hourly spawn budget reached ({policy.max_spawns_per_hour}/h) \u2014 skipping {action.kind} {','.join(action.quest_ids)}")
            notes.append({"quest": ",".join(action.quest_ids), "level": "skip",
                          "text": f"hourly spawn budget reached ({policy.max_spawns_per_hour}/h)"})
            continue
        label = f"{action.kind} {','.join(action.quest_ids)}" + (f" [{action.cogship}]" if action.cogship else "")
        if dry_run:
            say(f"[dry-run] would run: {label} \u2014 {action.reason}")
            executed.append({"action": action.__dict__, "result": {"ok": True, "dry_run": True}})
            continue
        say(f"\u25b6 {label} \u2014 {action.reason}")
        result = execute(repo, action)
        # Only a worker-spawning action spends the hourly budget: a local-git
        # `rebase`/`advance` vigil is zero-cost and must not exhaust the budget
        # that gates real dispatches and coins. Drain-lane spawns are also
        # recorded separately so plan() can settle their reserved hourly share.
        if _spends:
            _record_spawn(state, action, now)
            spawns = state["spawns"]
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
        "razed": [q["id"] for q in snap.get("quests", [])
                  if q.get("kind") != "epic" and q.get("razed_at")],
    })
    _write_pulse(repo, policy, snap, state, workers, notes, dry_run, say=say)
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


_LEDGER_DISPATCHED_RE = re.compile(
    r"^- \*\*(\d{4}-\d\d-\d\dT[\d:]+Z)\*\* — \S+ → DISPATCHED:", re.MULTILINE)


def dispatch_compliance(repo: Path, hours: int = 24) -> dict:
    """Classify recent dispatches as queue-driven vs direct (break-glass).

    The enqueue contract says routine new work goes through the Night's Watch
    queue; a dispatch is legitimate only when it is remediation, or when the
    watch is down (recorded as break-glass). Nothing enforced that, so measure
    it: a dispatch is "queued" when a `dispatch:<quest>` vigil fired within ten
    minutes of the ledger entry, and "direct" otherwise.
    """
    cutoff = time.time() - hours * 3600
    vigils: dict[str, list[float]] = {}
    try:
        from . import vigils as vigil_store

        for spec in vigil_store.load_all(repo, include_finished=True):
            action = str(spec.get("action") or spec.get("then") or "")
            if not action.startswith("dispatch:"):
                continue
            qid = action.split(":", 1)[1].split("-")[0].upper()
            for fire in spec.get("fires") or []:
                try:
                    ts = datetime.strptime(str(fire.get("ts")), "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc).timestamp()
                except (TypeError, ValueError):
                    continue
                if ts >= cutoff:
                    vigils.setdefault(qid, []).append(ts)
    except Exception:
        pass

    queued: list[tuple[str, float]] = []
    direct: list[tuple[str, float]] = []
    quests_dir = Path(repo) / ".court" / "quests"
    if quests_dir.is_dir():
        for path in sorted(quests_dir.glob("*.md")):
            qid = path.name.split("-")[0].upper()
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for match in _LEDGER_DISPATCHED_RE.finditer(text):
                try:
                    ts = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc).timestamp()
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                if any(abs(ts - fired) <= 600 for fired in vigils.get(qid, [])):
                    queued.append((qid, ts))
                else:
                    direct.append((qid, ts))
    return {"queued": queued, "direct": direct}


def _bucket_raze(repo: Path, qids: list) -> tuple[list, list]:
    """Split READY_TO_RAZE quests into (raze-ready, raze-refuses-other).

    Mirrors `court raze`'s exact gate: the quest BRANCH TIP must be reachable
    from castle (`git merge-base --is-ancestor`). Content may be present in the
    trunk while the branch tip is SHA-diverged (squash/merge), which raze
    refuses — those belong to a regate pass, not the raze sweep. Unknown/errored
    quests are kept on the raze-ready list (conservative; never hide one).
    """
    if not qids:
        return [], []
    try:
        from . import git_ops as _g
        from . import store as _s
    except Exception:
        return list(qids), []
    ready, regate = [], []
    quest_root = Path(repo) / ".court"
    for qid in qids:
        try:
            qp = _s.find_path(qid, quest_root)
            text = qp.read_text(encoding="utf-8") if qp and qp.exists() else ""
            m = re.search(r"^branch:\s*(\S+)", text, re.MULTILINE)
            branch = m.group(1) if m else ""
            if not branch:
                ready.append(qid)
                continue
            st = _g.check_merged_status(branch, target_ref="castle", base_ref="castle")
            merged = bool(st.get("is_merged_target") or st.get("is_merged_base"))
            (ready if merged else regate).append(qid)
        except Exception:
            ready.append(qid)  # conservative on any error
    return ready, regate


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
    # `start` rows are written ONCE at daemon boot (see tick's journal banner),
    # so counting them labels daemon restarts, not worker spawns. Call them what
    # they are: counting them "starts" read as "0 starts" forever once the boot
    # row rotated out of the retained window, and a Steward concluded the watch
    # had spawned nothing.
    boots = [r for r in rows if r.get("event") == "start"]
    spawned = [r for r in rows
               if r.get("event") == "action"
               and _journal_action_spawns(r.get("action") or {})]
    snaps = [r for r in rows if r.get("event") == "snapshot_failed"]
    lines.append(f"journal: {len(rows)} events · {len(actions)} actions · {len(spawned)} spawns · "
                 f"{len(boots)} boots · {len(snaps)} snapshot failures")
    if snaps:
        lines.append(f"  ⚠️ last snapshot failure: {snaps[-1].get('ts')} — {str(snaps[-1].get('error'))[:160]}")
    last_tick = next((r for r in reversed(rows) if r.get("event") == "tick"), None)
    if last_tick:
        cap = policy.get("max_workers") or "?"
        lines.append(f"capacity: {last_tick.get('workers', 0)}/{cap} live workers · "
                     f"{last_tick.get('rss_mb', 0)} MB RSS")

    # The Steward↔Watch hand-off: active dispatch vigils are the work queue.
    try:
        active_vigils = vigils.load_all(repo)
    except Exception:  # noqa: BLE001
        active_vigils = []
    dispatch_q = [v for v in active_vigils
                  if str(v.get("then") or "").startswith("dispatch:")]
    if dispatch_q:
        lines.append("")
        lines.append(f"dispatch queue: {len(dispatch_q)} hand-off(s) waiting for capacity")
        for v in dispatch_q[:12]:
            fires = len(v.get("fires") or [])
            lines.append(f"  🧵 {v['id']}: {v.get('then')} "
                         f"(fires {fires}/{v.get('max_fires', 1)}, expires {v.get('expires_at')})")
        if len(dispatch_q) > 12:
            lines.append(f"  … {len(dispatch_q) - 12} more")

    # --- Collect outcomes, grouped by the convoy they actually touched --------
    def _convoy_key(r):
        a = r["action"]
        key = a.get("cogship") or ""
        meta_convoy = (a.get("meta") or {}).get("convoy") or ""
        if not key and meta_convoy and meta_convoy != "new":
            key = meta_convoy
        if not key:
            blob = (r["result"].get("stdout_tail") or "") + (r["result"].get("stderr_tail") or "")
            found = re.search(r"cogship-\d+", blob)
            key = found.group(0) if found else ""
        return key or "unassigned"

    groups: dict[str, dict] = {}
    stale_wts: dict[str, str] = {}
    for r in actions:
        if r["action"]["kind"] != "collect":
            continue
        key = _convoy_key(r)
        g = groups.setdefault(key, {"packed": 0, "noop": 0, "failed": 0, "quests": set(),
                                    "first": None, "last": None, "reasons": set()})
        g["quests"].update(r["action"]["quest_ids"])
        g["first"] = g["first"] or r["ts"]
        g["last"] = r["ts"]
        out = r["result"].get("stdout_tail") or ""
        if r["result"].get("ok") and "Stamped" in out:
            g["packed"] += 1
        elif r["result"].get("ok"):
            g["noop"] += 1
            for line in out.splitlines():
                if "not ready to pack" in line or "UI Review is PENDING" in line:
                    g["reasons"].add(line.strip()[:120])
        else:
            g["failed"] += 1
            wt = _stale_convoy_worktree(r["result"])
            if wt:
                stale_wts[key] = wt
                g["reasons"].add("gatehouse worktree already exists (prior convoy died mid-integration)")

    # --- Stuck convoys: the authoritative view is the current state -----------
    stuck = dict(stale_wts)
    for key, convoy in (state.get("convoys") or {}).items():
        stale = (convoy or {}).get("stale") if isinstance(convoy, dict) else None
        if isinstance(stale, dict) and stale.get("worktree"):
            stuck[key] = stale["worktree"]
    if stuck:
        lines.append("")
        lines.append("stuck convoys — gatehouse worktree left by a dead convoy (tear down or regate):")
        for key, wt in sorted(stuck.items()):
            g = groups.get(key) or {}
            extra = (f" · {g.get('failed')} failed collect(s), {g.get('first')} → {g.get('last')}"
                     if g.get("failed") else "")
            lines.append(f"  ⛔ {key}: {wt}{extra}")
            lines.append(f"     clear with: git -C {repo} worktree remove --force {wt}")

    # --- READY_TO_RAZE awaiting a raze ---------------------------------------
    statuses = {}
    razed = set()
    statuses_authoritative = False
    for r in reversed(rows):
        if r.get("event") == "tick" and isinstance(r.get("statuses"), dict):
            statuses = r["statuses"]
            razed = set(r.get("razed") or [])
            statuses_authoritative = True
            break
    awaiting_raze = sorted(qid for qid, s in statuses.items()
                           if s == "READY_TO_RAZE" and qid not in razed)
    awaiting_prune = sorted(qid for qid, s in statuses.items()
                            if s == "READY_TO_RAZE" and qid in razed)
    # `court raze` demands the branch TIP be an ancestor of castle, not just
    # that content landed (a squash/merge leaves the quest branch SHA-diverged
    # from the promoted trunk, so raze refuses). Split the roster so the digest
    # never claims "promotion verified" for a quest raze will refuse —
    # Q1058/Q1083/Q1095/Q270/Q914 were listed raze-ready and refused 2026-11-03.
    raze_ready, raze_regate = _bucket_raze(repo, awaiting_raze)
    if raze_ready:
        lines.append("")
        lines.append("ready to raze — branch tip verified as an ancestor of castle (`court raze <id>`; opt in with `--enable ...,raze`):")
        for qid in raze_ready:
            lines.append(f"  🪦 {qid}")
    if raze_regate:
        lines.append("")
        lines.append("⚠️ ready to raze but raze will refuse — content is in castle but the branch tip is NOT an ancestor (squash/merge history):")
        for qid in raze_regate:
            lines.append(f"  🔁 {qid} — `court collect {qid} --regate --standup`, then raze")
    if awaiting_prune:
        lines.append("")
        lines.append("already razed — worktree awaiting M'Lord's manual prune (`court teardown-list`):")
        for qid in awaiting_prune:
            lines.append(f"  📦 {qid}")

    # --- Livelock: collects that exit 0 but pack nothing ----------------------
    livelocks = {k: v for k, v in groups.items() if v["noop"] >= 2}
    if livelocks:
        lines.append("")
        lines.append("livelock — collects that exit 0 but pack nothing (candidate fix: Court Artist / unblock):")
        for key, v in sorted(livelocks.items(), key=lambda kv: -kv[1]["noop"]):
            qs = ",".join(sorted(v["quests"]))
            lines.append(f"  ✗ convoy {key}: {v['noop']} no-op collect(s), {v['first']} → {v['last']}")
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

    # --- Merge-conflict blockers ------------------------------------------------
    # A coin/goad spawn blocked by `git merge castle` needs a STEWARD DECISION
    # (the watch's mechanical verbs cannot clear a rebase conflict). Surfaced
    # explicitly so the mustered Steward's prompt names the files. A terminal
    # (DONE/READY_TO_RAZE), razed, archived, or no-longer-in-the-ledger Quest is
    # NEVER "blocking a spawn": its marker is stale, so exclude it even if an
    # older state file still holds the record (the digest must not bury the live
    # blockers in that noise). Only treat the journal's active view as
    # authoritative when a tick row was actually found.
    _tracked = set((state.get("quests") or {}).keys())
    _stale = stale_conflict_ids(
        repo, statuses, razed,
        tracked_ids=_tracked if statuses_authoritative else None)
    conflicts = [
        (qid, st["merge_conflict"]) for qid, st in (state.get("quests") or {}).items()
        if isinstance(st.get("merge_conflict"), dict) and qid not in _stale
    ]
    if conflicts:
        lines.append("")
        lines.append("merge conflicts blocking a spawn (need a Steward to pick the resolution course):")
        for qid, mc in sorted(conflicts)[:15]:
            files = ", ".join((mc.get("files") or [])[:8])
            since = _iso_from_epoch(float(mc.get("ts") or 0)) if mc.get("ts") else "(ts missing)"
            esc = " · escalated" if mc.get("escalated_ts") else " · NOT yet escalated"
            lines.append(f"  ⚔️ {qid}: {files} (since {since}{esc})")
        if len(conflicts) > 15:
            lines.append(f"  … and {len(conflicts) - 15} more")

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

    steward = state.get("steward") or {}
    if steward:
        recent = [t for t in steward.get("spawns", []) if (time.time() - float(t)) < 86400]
        last = steward.get("last_ts")
        lines.append("")
        lines.append(f"steward musters (24h): {len(recent)}/{policy.get('max_stewards_per_day', '?')} · "
                     f"last {_iso_from_epoch(last) if last else '(never)'}")
        if steward.get("last_session"):
            lines.append(f"  last session: {steward['last_session']}")
        if steward.get("fails"):
            lines.append(f"  failed musters: {steward['fails']} — {str(steward.get('last_error') or '')[:160]}")

    compliance = dispatch_compliance(repo)
    lines.append("")
    if compliance["direct"]:
        lines.append(f"dispatch contract (24h): {len(compliance['queued'])} via queue · "
                     f"{len(compliance['direct'])} DIRECT (queue bypassed)")
        for qid, ts in compliance["direct"][-8:]:
            lines.append(f"  🔴 {_iso_from_epoch(ts)} {qid} dispatched without a dispatch vigil")
    else:
        lines.append(f"dispatch contract (24h): {len(compliance['queued'])} via queue · 0 direct ✅")

    if not stuck and not livelocks and not awaiting_raze and not awaiting_prune and not notified \
        and not conflicts and not spent and not fails:
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
            target = ",".join(a["quest_ids"]) or "(board)"
            lines.append(f"  {mark} {a['kind']} {target} — {a['reason']}")
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


LOCK_REFUSAL_NOTICE_INTERVAL = 60 * 60


def report_lock_refusal(repo: Path, why: str) -> None:
    """Announce a lost lock race at most once an hour.

    Two schedulers per repo is a normal shape (a tuned wrapper ticking beside a
    long-lived daemon, or a human's manual `--once`), and the per-repo lock
    already guarantees only one of them acts. Logging the refusal every tick
    buried real failures: 119 identical lines in pb-app and 85 in pb-custom by
    2026-11-02, all while the watch was healthy.
    """
    marker = state_dir(repo) / ".lock-refusal"
    now = time.time()
    try:
        last = float(marker.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        last = 0.0
    if now - last < LOCK_REFUSAL_NOTICE_INTERVAL:
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(now), encoding="utf-8")
    except OSError:
        pass
    print(f"ERROR: {why}", file=sys.stderr)


def run_loop(repo: Path, policy: Policy, interval: int, once: bool, dry_run: bool, quiet: bool = False) -> int:
    ok, why = acquire_instance_lock(repo)
    if not ok:
        report_lock_refusal(repo, why)
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
    banner = policy.describe()
    previous = state.get("policy_line") or ""
    state["policy_line"] = banner
    state["policy"] = dict(policy.__dict__)
    state["pid"] = os.getpid()
    save_state(repo, state)
    say(f"🧭 policy (pid {os.getpid()}): {banner}")
    if not once:
        _journal(repo, {"ts": _now_iso(), "event": "start", "pid": os.getpid(), "dry_run": dry_run,
                        "policy": policy.__dict__, "policy_line": banner, "interval": interval, "once": once})
    elif banner != previous:
        # A wrapper ticking every few minutes would otherwise write a start line
        # per tick (159 of them in pb-custom); record the policy only when it
        # actually changes.
        _journal(repo, {"ts": _now_iso(), "event": "policy", "pid": os.getpid(),
                        "policy_line": banner, "previous": previous})
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
        if not once:
            _journal(repo, {"ts": _now_iso(), "event": "stop", "pid": os.getpid(),
                            "reason": "signal" if stop["flag"] else "done"})
        release_instance_lock(repo)
    return rc
