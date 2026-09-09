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
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import store
from .models import Quest, STATUSES, SECTIONS, KINDS, now_iso, validate_branch_name, status_label
from . import git_ops
from . import ward
from . import branch_ops
from . import config

# The Ward's durable workspace: patrol ledger + Warden Report queue.
WARD_DIR = Path(__file__).resolve().parent.parent / "ward"
WARDENS_LOG_PATH = WARD_DIR / "WARDENS_LOG.md"
WARD_REPORTS_DIR = WARD_DIR / "reports"

# Role model defaults loaded from .court/config.json with standard fallbacks
_CFG = config.load_config()
DEFAULT_SERF_MODEL = _CFG["models"].get("serf", "GLM-5.3-Flash")
SERF_PROVIDER = _CFG["models"].get("serf_provider", "openrouter")
DEFAULT_MOC_MODEL = _CFG["models"].get("master_of_coin", "openrouter/google/gemini-3.8-flash")
DEFAULT_GATEKEEPER_MODEL = _CFG["models"].get("gatekeeper", "openrouter/google/gemini-3.8-flash")
DEFAULT_ARTIST_MODEL = _CFG["models"].get("artist", "openrouter/z-ai/glm-5.3")
ARTIST_PROVIDER = _CFG["models"].get("artist_provider", "openrouter")
SERF_DISPATCH_TEMPLATE = ".court/templates/serf_dispatch_prompt.md"
ARTIST_DISPATCH_TEMPLATE = ".court/templates/court_artist_prompt.md"
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


def canonical_model_id(model_str: str, provider: Optional[str] = None) -> str:
    """Map human/display model names to fully qualified provider/model strings for Kilo CLI."""
    if not model_str:
        return "openrouter/z-ai/glm-5.3-flash"
    m = model_str.strip()
    if "/" in m:
        return m
    low = m.lower().replace(" ", "").replace("-", "").replace(".", "")
    if "glm53flash" in low:
        return "openrouter/z-ai/glm-5.3-flash"
    if "glm53" in low:
        return "openrouter/z-ai/glm-5.3"
    if "gemini38flash" in low:
        return "openrouter/google/gemini-3.8-flash"
    if "gemini37flash" in low:
        return "openrouter/google/gemini-3.7-flash"
    p = provider or "openrouter"
    return f"{p}/{m}"


def query_latest_kilo_session_id(worktree_path: Path, timeout_seconds: float = 2.5) -> Optional[str]:
    """Inspect local kilo.db to find the session ID created for a worktree."""
    db_path = Path.home() / ".local" / "share" / "kilo" / "kilo.db"
    wt_str = str(worktree_path.resolve())
    deadline = time.time() + timeout_seconds
    while time.time() <= deadline:
        if db_path.is_file():
            try:
                conn = sqlite3.connect(str(db_path), timeout=1.0)
                cur = conn.cursor()
                cur.execute(
                    "SELECT id FROM session WHERE directory = ? ORDER BY time_created DESC LIMIT 1",
                    (wt_str,)
                )
                row = cur.fetchone()
                conn.close()
                if row and row[0]:
                    return row[0]
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
) -> dict:
    """Stand up a Kilo session in a worktree with explicit agent mode.

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
            qual_model = canonical_model_id(model, provider=SERF_PROVIDER)
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
                    prompt,
                ]
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(worktree_path),
                    stdout=log_out,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            finally:
                log_out.close()

            session_id = query_latest_kilo_session_id(worktree_path, timeout_seconds=2.5) or f"kilo-{agent}-{proc.pid}"
            return {
                "ok": True,
                "mode": "cli",
                "session_id": session_id,
                "pid": proc.pid,
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
        "(defaults to the standing GLM 5.3 Flash mandate)"
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
                "worktree": audit.worktree,
                "serf_session_id": q.serf_session_id,
                "serf_model": q.serf_model,
                "master_of_coin_session_id": q.master_of_coin_session_id,
                "gatekeeper_session_id": q.gatekeeper_session_id,
                "parent_epic": q.parent_epic,
                "scout_of": getattr(q, "scout_of", ""),
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
        print(f"Note: {len(orphans)} orphaned worktrees exist in Agent Manager (court timber / court raze / court fork-teardown-list available for cleanup).")


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
                # Q185 truthfulness fix: --force alone used to be enough to
                # declare a merge happened without it actually happening —
                # the resulting ledger note looked identical to a real
                # promotion to every downstream consumer (2026-09-05
                # incident: 9 Quests force-advanced to READY_TO_RAZE, none
                # were actually merged; caught only because the Steward
                # happened to independently re-check merge-base by hand).
                # A verified commit SHA is now required, and independently
                # re-checked against castle — the caller can no longer just
                # assert it, the CLI verifies it.
                if not verified_commit:
                    print(
                        f"ERROR: --force on an unmerged READY_TO_RAZE also requires --verified-commit <sha> "
                        f"naming the commit you're claiming is actually merged — the CLI independently "
                        f"re-verifies it against castle rather than taking your word for it.\n\n"
                        f"Recommendation: {merge_res.get('recommendation')}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                proof = git_ops.verify_commit_is_ancestor(verified_commit, "castle")
                if not proof.get("is_ancestor"):
                    print(
                        f"ERROR: --verified-commit {verified_commit} failed independent verification: "
                        f"{proof.get('error')}\n"
                        f"--force is refused — this is not a real merge, it cannot be declared into one.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                forced_note = (
                    f"(FORCED - UNMERGED, but --verified-commit {proof['resolved_sha'][:12]} independently "
                    f"confirmed as an ancestor of castle) {args.note}"
                ).strip()
                quest.set_status(new_status, forced_note)
                quest.cogship_promoted_commit = proof["resolved_sha"]
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
            f'   kilo --worktree {wt_name} --agent serf --model "{DEFAULT_SERF_MODEL}"',
            f'   or:',
            f'   kilo worktree create {wt_name}',
            f'   kilo run --agent serf --model "{DEFAULT_SERF_MODEL}" --dir <WORKTREE_PATH> "<TASK_PROMPT>"',
            f"",
            f"2. Or if manually spawning an Agent Manager UI session in section \"{section_display}\":",
            f"   python3 -m court.cli dispatch-complete {quest.id} \\",
            f"       --session-id <SESSION_ID> --branch {branch} --worktree <WORKTREE_PATH>",
        ],
    )


def cmd_charter(args):
    """Composite charter (Q183): fold M'Lord's notes into `The Kingdom Requires`,
    idempotently advance OPEN -> PLANNED, compute the canonical branch when
    unset, and print the NEXT STEPS block for the irreducible `agent_manager`
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
    serf_model = getattr(args, "serf_model", None) or DEFAULT_SERF_MODEL

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
    if quest.status in ("OPEN", "PLANNED"):
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

    model = getattr(args, "model", None) or DEFAULT_MOC_MODEL
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
        rendered_prompt,
    ]

    auto_commit = not getattr(args, "no_commit", False)
    wait = getattr(args, "wait", False)

    if wait:
        print(f"🪙 Running Master of Coin audit for {quest.id} synchronously (agent: master_of_coin, model: {qual_model})...")
        res = subprocess.run(cmd, cwd=str(wt))
        session_id = query_latest_kilo_session_id(wt)
        if session_id:
            quest.master_of_coin_session_id = session_id
            quest.master_of_coin_model = model
            store.save(quest, auto_commit=auto_commit, commit_msg=f"court: record MoC session {session_id} for {quest.id}")
        return res.returncode
    else:
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
            )
        finally:
            log_out.close()
        session_id = query_latest_kilo_session_id(wt, timeout_seconds=2.5) or f"kilo-coin-{proc.pid}"
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
    model = getattr(args, "model", None) or quest.serf_model or DEFAULT_SERF_MODEL
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
        rendered_prompt,
    ]

    auto_commit = not getattr(args, "no_commit", False)
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
        )
    finally:
        log_out.close()
    session_id = query_latest_kilo_session_id(wt, timeout_seconds=2.5) or f"kilo-{agent_role}-{proc.pid}"
    quest.serf_session_id = session_id
    store.save(quest, auto_commit=auto_commit, commit_msg=f"court: goad {role_label} session {session_id} for {quest.id}")
    print(f"⚡ Goaded {role_label} for {quest.id}:")
    print(f"   Session:  {session_id} (PID {proc.pid})")
    print(f"   Agent:    {agent_role}")
    print(f"   Model:    {qual_model}")
    print(f"   Worktree: {wt}")
    print(f"   Log:      tail -f {log_file}")


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

    model = getattr(args, "model", None) or quest.artist_model or DEFAULT_ARTIST_MODEL
    provider = getattr(args, "provider", None) or ARTIST_PROVIDER
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
    task_desc = {
        "name": f"{short_id} Court Artist",
        "branchName": quest.branch,
        "model": model,
        "provider": provider,
        "prompt": prompt,
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
            "task": task_desc,
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
    print(f"  1. Launch the Court Artist session in Agent Manager:")
    print(f"     agent_manager start (mode: 'worktree', branchName: '{quest.branch}', model: '{model}', name: '{short_id} Court Artist')")
    print(f"     (Slash command: `/artist {quest.id}` spawns this automatically).")
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
        f"Merged in Target: {'✅ YES' if status.get('is_merged_in_target') else '❌ NO'} (ancestor={status.get('is_ancestor_target')})",
        f"Merged in Base:   {'✅ YES' if status.get('is_merged_in_base') else '❌ NO'} (ancestor={status.get('is_ancestor_base')})",
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

            if res.get("is_merged") and res.get("is_merged_in_base"):
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
                dst = store.archive(quest.id)
                print(f"🪦 {quest.id}: Worktree already pruned from disk/AM -> Archived to {dst}")
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
        is_merged = status.get("is_merged_in_target") or status.get("is_merged_in_base")

        if not is_merged and not is_scout:
            unmerged_count = status.get("unmerged_commits_count", 0)
            print(
                f"❌ {quest.id}: NOT merged into castle (verified via merge-base ancestor check; "
                f"{unmerged_count} unmerged commit(s)). Refusing to raze. Route through Gatekeeper first."
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
            print(f"   👉 Move command: agent_manager move sessionID: {found_session_id} sectionID: {ashes_section_id}")

        # This Quest is a pillory successor: raze its frozen predecessor
        # together with it, in the same pass, per the Pillory protocol.
        if quest.pillory_of:
            print(f"   🔗 {quest.id} is a pillory successor of {quest.pillory_of} — razing it together now.")
            _raze_one(quest.pillory_of)

    for qid in target_ids:
        _raze_one(qid)


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
    for quest in candidates:
        if quest.status == "PUNISHED":
            skipped.append((quest.id, "PUNISHED (side-state, frozen pending its pillory successor)"))
            continue
        if quest.status not in ("TRIBUTE_READY", "GATE"):
            skipped.append((quest.id, f"status is [{quest.status}], not TRIBUTE_READY or GATE"))
            continue
        moc_audit = quest.body_sections.get("Master of Coin's Audit", "").strip()
        if not moc_audit:
            skipped.append((
                quest.id,
                "no recorded Master of Coin's Audit content -- not yet reviewed; refusing to "
                "pack unaudited tribute into a convoy (dispatch `/levy <id>` first)",
            ))
            continue
        ui_status = quest.extract_ui_review_status()
        if ui_status.upper().startswith("PENDING") and not getattr(args, "skip_ui_review", False):
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
        accepted.append(quest)

    if skipped:
        print(f"⏭️  Skipped {len(skipped)} candidate(s) (not ready to pack):")
        for qid, reason in skipped:
            print(f"   - {qid}: {reason}")

    if not accepted:
        print("(no Master-of-Coin-approved Quests ready to pack into a Cog Ship)")
        return

    cogship_id = store.stamp_cogship(accepted, cogship_id=getattr(args, "cogship", None), auto_commit=auto_commit)
    print(f"🚢 Stamped {len(accepted)} Quest(s) onto {cogship_id}.")

    for quest in accepted:
        quest.set_status("GATE", f"Packed in {cogship_id}; dispatched to Gatekeeper")
        store.save(quest, auto_commit=auto_commit, commit_msg=f"court: advance {quest.id} to GATE")
        print(f"{quest.id}: GATE")

    accepted_ids = ", ".join(q.id for q in accepted)
    qual_gatekeeper = canonical_model_id(DEFAULT_GATEKEEPER_MODEL)
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
            f"   python3 -m court.cli set-field {solo_id} gatekeeper_model \"{DEFAULT_GATEKEEPER_MODEL}\"",
            "",
            "2. On a clean suite run, promote directly into castle and advance to",
            "   READY_TO_RAZE.",
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
            "2. Record the session on each packed Quest:",
            f"   python3 -m court.cli set-field <id> gatekeeper_session_id <session_id>",
            f"   python3 -m court.cli set-field <id> gatekeeper_model \"{DEFAULT_GATEKEEPER_MODEL}\"",
            "",
            "3. Tear the ephemeral worktree down once promoted.",
        ]

    if getattr(args, "standup", False):
        kilo_bin = find_kilo_binary()
        if kilo_bin:
            if len(accepted) == 1:
                solo = accepted[0]
                if solo.worktree and Path(solo.worktree).is_dir():
                    wt = Path(solo.worktree)
                    setup_worktree_agent_config(wt, "gatekeeper")
                    prompt = f"Act as Gatekeeper for {solo.id} on {cogship_id}: merge castle in, run test suite, and on clean pass merge into castle."
                    res = standup_kilo_session(wt, agent="gatekeeper", model=qual_gatekeeper, prompt=prompt, title=f"{cogship_id} Gatekeeper", kilo_bin=kilo_bin, run_now=True)
                    sid = res.get("session_id")
                    if sid:
                        solo.gatekeeper_session_id = sid
                        solo.gatekeeper_model = DEFAULT_GATEKEEPER_MODEL
                        store.save(solo, auto_commit=auto_commit, commit_msg=f"court: record Gatekeeper {sid} for {solo.id}")
                        print(f"🛡️ Stood up Gatekeeper session {sid} for {solo.id} via Kilo CLI.")
            else:
                root = git_ops.get_repo_root()
                wt_name = f"the-gatehouse-{cogship_id}"
                branch_name = f"the-gatehouse/{cogship_id}"
                wt_path = root / ".kilo" / "worktrees" / wt_name
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
                    res = standup_kilo_session(wt_path, agent="gatekeeper", model=qual_gatekeeper, prompt=prompt, title=f"{cogship_id} Gatekeeper", kilo_bin=kilo_bin, run_now=True)
                    sid = res.get("session_id")
                    if sid:
                        for q in accepted:
                            q.gatekeeper_session_id = sid
                            q.gatekeeper_model = DEFAULT_GATEKEEPER_MODEL
                            store.save(q, auto_commit=auto_commit, commit_msg=f"court: record Gatekeeper {sid} for {q.id}")
                        print(f"🛡️ Stood up Gatekeeper convoy session {sid} on {branch_name} via Kilo CLI.")
                except Exception as e:
                    print(f"⚠️ Could not auto-standup Gatekeeper: {e}")

    _print_next_steps(f"🛡️ NEXT STEPS — SUMMON THE GATEKEEPER FOR {cogship_id}", next_steps)


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
        stamped = store.stamp_cogship(quests, cogship_id=cogship_id, auto_commit=auto_commit)
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
        print(f"\n🔥 Resting in Ashes Section (Safe for M'Lord to delete in Agent Manager UI) ({len(active_in_ashes)}):")
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
        print(f"\n📦 Already Pruned from Agent Manager / Disk ({len(already_pruned)} Quests ready to archive):")
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
    print("remove` (M'Lord prunes the directory by hand in the Agent Manager UI).\n")

    if moc_eligible:
        print(f"✅ ELIGIBLE — Master of Coin forks, verdict synced, safe to move+stop ({len(moc_eligible)}):")
        for e in moc_eligible:
            q = e["quest"]
            target = treasury_id or "<create a 'The Treasury' or 'Ashes' section first>"
            print(f"   * {q.id} [{q.status}] — {e['branch']}")
            print(f"     {e['reason']}")
            if e["session_id"]:
                print(f"     👉 agent_manager move sessionID={e['session_id']} sectionID={target}")
                print(f"     👉 agent_manager stop sessionID={e['session_id']}   (AFTER the move above lands)")
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
                print(f"     👉 agent_manager prompt sessionID={e['session_id']}: \"Confirm your verdict is written under "
                      f"## Master of Coin's Audit in .court/quests/{q.id}*.md, then run `git push . HEAD:{q.branch}` "
                      f"from this fork worktree and report back the exact push result. Do not call agent_manager stop "
                      f"yourself.\"")
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
                print(f"     👉 agent_manager move sessionID={e['session_id']} sectionID={ashes_id or '<Ashes section id>'}")
                print(f"     👉 agent_manager stop sessionID={e['session_id']}   (AFTER the move above lands)")
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
                    auto_resolved = rb.get("auto_resolved_foreign_ledger_files") or []
                    foreign_note = f" (auto-resolved {len(auto_resolved)} foreign ledger conflict(s) to {base_branch}'s side)" if auto_resolved else ""
                    rebase_notes[q.id] = (
                        f"🔄 Mechanically rebased onto {base_branch}: "
                        f"{rb.get('before_behind')} -> {rb.get('after_behind')} behind.{foreign_note}"
                    )
                elif rb.get("conflict"):
                    sample = ", ".join(rb.get("conflict_files", [])[:5]) or "unknown files"
                    rebase_notes[q.id] = (
                        f"⚠️  Mechanical rebase onto {base_branch} hit conflicts in: {sample} "
                        f"— aborted cleanly, needs Serf resolution (`git merge {base_branch}` by hand)."
                    )
                elif rb.get("skipped") == "dirty":
                    rebase_notes[q.id] = "⚠️  Skipped mechanical rebase: worktree has uncommitted changes."

        # Step 1: Auto-sync tribute and frontmatter from worktree if present
        if getattr(args, "sync", True):
            ward.sync_tribute_from_worktree(q, worktree_path=wt)

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
                store.save(q)
                did_advance = True

        if audit.violations:
            violations_list.append((q, audit))
        elif q.status == "TRIBUTE_READY" or did_advance:
            levied_list.append((q, audit, did_advance))
        else:
            working_list.append((q, audit))

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
    print("  • For WORKING Quests genuinely idle/incomplete (checklist < 100% and/or dirty tree): Run `/goad <quest_id>` or prod the session via Agent Manager to update charter & continue.")
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
            auto_resolved = r.get("auto_resolved_foreign_ledger_files") or []
            foreign_note = f"  [auto-resolved {len(auto_resolved)} foreign ledger conflict(s) -> {base_branch}]" if auto_resolved else ""
            print(f"  🔄 {qid:36} merged: {r.get('before_behind')} -> {r.get('after_behind')} behind.{foreign_note}  {title}")
        elif r.get("conflict"):
            sample = ", ".join(r.get("conflict_files", [])[:5]) or "unknown files"
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
        help="Override the Agent Manager section lane for the dispatch NEXT STEPS",
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
        default=DEFAULT_SERF_MODEL,
        help="Worker model",
    )
    p_dispatch.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_dispatch.set_defaults(func=cmd_dispatch)

    p_coin = sub.add_parser(
        "coin",
        help="Dispatch Master of Coin into a Quest's worktree via Kilo CLI to audit Tribute",
    )
    p_coin.add_argument("quest_id", help="Quest ID to audit (must be in TRIBUTE_READY unless --force)")
    p_coin.add_argument("--model", default=DEFAULT_MOC_MODEL, help=f"Model for Master of Coin (default: {DEFAULT_MOC_MODEL})")
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
    p_dispatch_complete.add_argument("--session-id", required=True, dest="session_id", help="Serf Agent Manager session id returned by the agent_manager tool call")
    p_dispatch_complete.add_argument("--branch", required=True, help="Canonical Quest branch (slash hierarchy)")
    p_dispatch_complete.add_argument("--worktree", required=True, help="Quest worktree path returned by the agent_manager tool call")
    p_dispatch_complete.add_argument(
        "--model", "--serf-model",
        dest="serf_model",
        default=DEFAULT_SERF_MODEL,
        help="Serf model (default: the standing GLM 5.3 Flash mandate; --model and --serf-model are aliases)",
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
        default=DEFAULT_ARTIST_MODEL,
        help=f"Model for Court Artist (default: {DEFAULT_ARTIST_MODEL})",
    )
    p_artist.add_argument(
        "--provider",
        default=ARTIST_PROVIDER,
        help=f"Provider for Court Artist model (default: {ARTIST_PROVIDER})",
    )
    p_artist.add_argument("--port", type=int, help="Override worktree runserver port")
    p_artist.add_argument("--no-server", action="store_true", help="Skip starting the worktree dev server")
    p_artist.add_argument("--no-commit", action="store_true", help="Do not autocommit quest updates")
    p_artist.add_argument("--prompt-only", action="store_true", help="Print only the rendered Court Artist prompt")
    p_artist.add_argument("--json", action="store_true", help="Output JSON format for agent_manager or scripts")
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
    p_raze.set_defaults(func=cmd_raze)

    p_teardown = sub.add_parser("teardown-list", help="List worktrees ready for M'Lord to prune")
    p_teardown.set_defaults(func=cmd_teardown_list)

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
    p_collect.add_argument("--standup", action="store_true", help="Automatically stand up the Gatekeeper worktree and session via Kilo CLI")
    p_collect.add_argument("--force", action="store_true", help="Force packing even if checks warn/fail")
    p_collect.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
    p_collect.set_defaults(func=cmd_collect)

    p_stamp = sub.add_parser("stamp", help="Stamp Quests onto a Cog Ship convoy id (allocates next cogship-NNN unless --cogship given)")
    p_stamp.add_argument("quest_ids", help="Comma-separated Quest IDs (e.g. Q101,Q102,Q105)")
    p_stamp.add_argument("--cogship", default=None, help="Existing Cog Ship ID to stamp (e.g. cogship-002); omit or pass 'new' to allocate the next id")
    p_stamp.add_argument("--no-commit", action="store_true", help="Do not autocommit changes to git")
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


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
