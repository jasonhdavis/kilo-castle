"""
Read-only per-Quest metrics rollup: cost, wall-clock, turns, and goads.

Two independent sinks are folded together, per Quest:

  * ``kilo.db`` (``~/.local/share/kilo/kilo.db``) — the authoritative record of
    every worker session bound to the Quest's worktree(s): provider-reported
    ``cost``, token accounting, wall-clock timestamps, and the per-message turn
    counts.
  * the Court ledger — the append-only worktree logs (``.kilo/<role>.log``)
    where every explicit goad appends a ``--- Goad <Role>: ...`` marker, plus
    the Quest's registered worktree / branch / convoy worktrees.

Every function here is strictly read-only: the sqlite connection is opened with
``mode=ro`` (never a write, never a migration), no Quest/Epic file, event log,
or git ref is touched, and nothing is committed. This is the live-pipeline twin
of the offline model bake-off: the same four measurements (cost, wall-clock,
turns, goads) that let a bake-off rank models, computed for real Quests so the
switch can be judged on data instead of vibes.

Session→Quest binding:
  1. the explicit per-role session ids recorded on the Quest
     (``serf_session_id``, ``master_of_coin_session_id``, ...), and
  2. every session whose ``directory`` is one of the Quest's worktrees
     (the registered ``worktree``, the branch-derived worktree dir, and the
     Cog Ship convoy's ephemeral gatehouse worktree),
  3. plus the transitive set of child sessions (``parent_id``) of any bound
     session, so sub-agent work counts toward the Quest that spawned it.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

from . import store

DEFAULT_KILO_DB = "~/.local/share/kilo/kilo.db"

# Explicit per-role session ids on the Quest frontmatter, in role order.
ROLE_SESSION_FIELDS = (
    ("serf", "serf_session_id"),
    ("scout", "serf_session_id"),  # scouts reuse the serf session field
    ("master_of_coin", "master_of_coin_session_id"),
    ("gatekeeper", "gatekeeper_session_id"),
    ("artist", "artist_session_id"),
    ("vassal", "vassal_session_id"),
)

# Every explicit goad appends this marker to the worktree log (see cmd_goad /
# cmd_artist / cmd_coin). It is the durable goad ledger.
_GOAD_MARKER_RE = re.compile(r"^--- Goad ", re.MULTILINE)
# kilo.db session titles of the shape "<ID> <Role> Goad" corroborate the logs.
_GOAD_TITLE_RE = re.compile(r"\bgoad\b", re.IGNORECASE)


def kilo_db_path(db_path: Optional[Path | str] = None) -> Path:
    """Resolve the local Kilo session database path.

    Precedence: explicit ``db_path`` argument, ``COURT_KILO_DB`` environment
    override (for tests / alternate installs), then the standard user path.
    """
    if db_path:
        return Path(db_path).expanduser()
    env = os.environ.get("COURT_KILO_DB")
    if env:
        return Path(env).expanduser()
    return Path(DEFAULT_KILO_DB).expanduser()


def _norm(path: Path | str) -> str:
    """Normalize a filesystem path for equality comparison without touching
    disk (worktrees may not exist anymore, so ``resolve()`` is too strict)."""
    return os.path.normpath(os.path.expanduser(str(path)))


def _repo_root(court_root: Path) -> Path:
    return court_root.parent


def quest_worktrees(quest, court_root: Optional[Path] = None) -> list[str]:
    """Return the normalized directories a Quest's sessions may live in."""
    court_root = court_root or store.get_court_root()
    root = _repo_root(court_root)
    candidates: list[Path] = []
    if getattr(quest, "worktree", ""):
        candidates.append(Path(quest.worktree))
    branch = getattr(quest, "branch", "") or ""
    if branch:
        candidates.append(root / ".kilo" / "worktrees" / branch.replace("/", "-"))
    cog = store.normalize_cogship_id(getattr(quest, "cogship_id", ""))
    if cog:
        candidates.append(root / ".kilo" / "worktrees" / f"the-gatehouse-{cog}")

    out: list[str] = []
    for c in candidates:
        n = _norm(c)
        if n not in out:
            out.append(n)
    return out


def _explicit_session_ids(quest) -> set[str]:
    ids: set[str] = set()
    for _role, field in ROLE_SESSION_FIELDS:
        val = (getattr(quest, field, "") or "").strip()
        if val:
            ids.add(val)
    return ids


def _open_ro(db_path: Path) -> Optional[sqlite3.Connection]:
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _load_sessions(conn: sqlite3.Connection) -> dict[str, dict]:
    cur = conn.execute(
        "SELECT id, parent_id, directory, agent, model, title, "
        "time_created, time_updated, cost, "
        "tokens_input, tokens_output, tokens_reasoning, "
        "tokens_cache_read, tokens_cache_write "
        "FROM session"
    )
    sessions: dict[str, dict] = {}
    for row in cur.fetchall():
        d = dict(row)
        d["_dir"] = _norm(d.get("directory") or "")
        sessions[d["id"]] = d
    return sessions


def _descendants(sessions: dict[str, dict], seeds: set[str]) -> set[str]:
    children: dict[str, list[str]] = {}
    for sid, s in sessions.items():
        parent = s.get("parent_id") or ""
        if parent:
            children.setdefault(parent, []).append(sid)
    out = set(seeds)
    stack = list(seeds)
    while stack:
        cur = stack.pop()
        for child in children.get(cur, []):
            if child not in out:
                out.add(child)
                stack.append(child)
    return out


def _message_counts(conn: sqlite3.Connection, session_ids: Iterable[str]) -> dict[str, dict]:
    ids = [s for s in session_ids if s]
    if not ids:
        return {}
    out: dict[str, dict] = {}
    # Chunk the IN-list so a huge Quest set can never blow the SQLite variable
    # limit; the message(session_id) index keeps each chunk cheap.
    chunk = 400
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        qmarks = ",".join("?" * len(batch))
        cur = conn.execute(
            f"SELECT session_id, COUNT(*) AS total, "
            f"SUM(json_extract(data,'$.role') = 'user') AS users, "
            f"SUM(json_extract(data,'$.role') = 'assistant') AS assistants "
            f"FROM message WHERE session_id IN ({qmarks}) GROUP BY session_id",
            tuple(batch),
        )
        for row in cur.fetchall():
            out[row["session_id"]] = {
                "messages": int(row["total"] or 0),
                "turns": int(row["users"] or 0),
                "assistant_messages": int(row["assistants"] or 0),
            }
    return out


def _goad_count_from_logs(worktrees: Iterable[str]) -> tuple[int, int]:
    """Count ``--- Goad ...`` markers across the worktrees' ``.kilo/*.log``
    files. Returns (log_marker_count, logs_scanned)."""
    total = 0
    scanned = 0
    for wt in worktrees:
        log_dir = Path(wt) / ".kilo"
        if not log_dir.is_dir():
            continue
        for log in sorted(log_dir.glob("*.log")):
            scanned += 1
            try:
                text = log.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            total += len(_GOAD_MARKER_RE.findall(text))
    return total, scanned


def _empty_metrics(quest) -> dict:
    return {
        "quest_id": quest.id,
        "title": getattr(quest, "title", ""),
        "status": getattr(quest, "status", ""),
        "kind": getattr(quest, "kind", ""),
        "app": getattr(quest, "app", ""),
        "cogship_id": getattr(quest, "cogship_id", ""),
        "models": [],
        "sessions": 0,
        "cost_usd": 0.0,
        "wall_clock_seconds": 0.0,
        "active_seconds": 0.0,
        "turns": 0,
        "assistant_messages": 0,
        "messages": 0,
        "tokens": {
            "input": 0, "output": 0, "reasoning": 0,
            "cache_read": 0, "cache_write": 0,
        },
        "goads": 0,
        "goads_log": 0,
        "goads_db": 0,
        "logs_scanned": 0,
        "first_seen": "",
        "last_seen": "",
        "worktrees": [],
        "by_role": {},
        "by_model": {},
    }


def _iso(ms: Optional[int]) -> str:
    if not ms:
        return ""
    import datetime
    try:
        return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (OverflowError, OSError, ValueError):
        return ""


def quest_metrics(
    quest,
    *,
    court_root: Optional[Path] = None,
    all_sessions: Optional[dict[str, dict]] = None,
    all_messages: Optional[dict[str, dict]] = None,
) -> dict:
    """Compute the read-only rollup for a single Quest.

    ``all_sessions`` / ``all_messages`` are the pre-loaded kilo.db maps from
    :func:`rollup_metrics` (one shared scan, not one per Quest). When omitted,
    session metrics read zero but the log-ledger goad count still runs.
    """
    court_root = court_root or store.get_court_root()
    m = _empty_metrics(quest)
    wts = quest_worktrees(quest, court_root)
    m["worktrees"] = wts

    sess_map = all_sessions or {}
    msg_map = all_messages or {}

    seeds: set[str] = set()
    for sid in _explicit_session_ids(quest):
        if sid in sess_map:
            seeds.add(sid)
    for sid, s in sess_map.items():
        if s.get("_dir") and s["_dir"] in wts:
            seeds.add(sid)

    bound = _descendants(sess_map, seeds) if sess_map else set()

    models: set[str] = set()
    by_role: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    min_created: Optional[int] = None
    max_updated: Optional[int] = None

    for sid in bound:
        s = sess_map[sid]
        cost = float(s.get("cost") or 0.0)
        created = s.get("time_created") or 0
        updated = s.get("time_updated") or 0
        model = _model_label(s.get("model"))
        if model:
            models.add(model)
        role = (s.get("agent") or "").strip() or "unknown"

        mc = msg_map.get(sid) or {"messages": 0, "turns": 0, "assistant_messages": 0}
        duration = max(0, (updated - created)) / 1000.0

        m["sessions"] += 1
        m["cost_usd"] += cost
        m["active_seconds"] += duration
        m["turns"] += mc["turns"]
        m["assistant_messages"] += mc["assistant_messages"]
        m["messages"] += mc["messages"]
        m["tokens"]["input"] += int(s.get("tokens_input") or 0)
        m["tokens"]["output"] += int(s.get("tokens_output") or 0)
        m["tokens"]["reasoning"] += int(s.get("tokens_reasoning") or 0)
        m["tokens"]["cache_read"] += int(s.get("tokens_cache_read") or 0)
        m["tokens"]["cache_write"] += int(s.get("tokens_cache_write") or 0)

        if created:
            min_created = created if min_created is None else min(min_created, created)
        if updated:
            max_updated = updated if max_updated is None else max(max_updated, updated)

        r = by_role.setdefault(
            role,
            {"sessions": 0, "cost_usd": 0.0, "active_seconds": 0.0,
             "turns": 0, "messages": 0, "goads": 0},
        )
        r["sessions"] += 1
        r["cost_usd"] += cost
        r["active_seconds"] += duration
        r["turns"] += mc["turns"]
        r["messages"] += mc["messages"]

        mdl = model or "(unknown)"
        mb = by_model.setdefault(
            mdl,
            {"sessions": 0, "cost_usd": 0.0, "active_seconds": 0.0,
             "turns": 0, "messages": 0, "goads": 0},
        )
        mb["sessions"] += 1
        mb["cost_usd"] += cost
        mb["active_seconds"] += duration
        mb["turns"] += mc["turns"]
        mb["messages"] += mc["messages"]

        if _GOAD_TITLE_RE.search(s.get("title") or ""):
            r["goads"] += 1
            mb["goads"] += 1
            m["goads_db"] += 1

    if min_created is not None and max_updated is not None:
        m["wall_clock_seconds"] = max(0, (max_updated - min_created)) / 1000.0
        m["first_seen"] = _iso(min_created)
        m["last_seen"] = _iso(max_updated)

    goad_log, logs_scanned = _goad_count_from_logs(wts)
    m["goads_log"] = goad_log
    m["logs_scanned"] = logs_scanned
    # The log ledger is authoritative when present; kilo.db session titles are
    # the corroborating fallback when a worktree's logs have been reaped.
    m["goads"] = goad_log if goad_log else m["goads_db"]

    m["models"] = sorted(models)
    m["cost_usd"] = round(m["cost_usd"], 6)
    m["wall_clock_seconds"] = round(m["wall_clock_seconds"], 3)
    m["active_seconds"] = round(m["active_seconds"], 3)
    for r in by_role.values():
        r["cost_usd"] = round(r["cost_usd"], 6)
        r["active_seconds"] = round(r["active_seconds"], 3)
    for r in by_model.values():
        r["cost_usd"] = round(r["cost_usd"], 6)
        r["active_seconds"] = round(r["active_seconds"], 3)
    m["by_role"] = by_role
    m["by_model"] = by_model
    return m


def _model_label(model_raw: Any) -> str:
    """kilo.db ``model`` is a JSON blob ``{"id","providerID","variant"}`` (or a
    plain string on older rows). Return the human model id."""
    if not model_raw:
        return ""
    if isinstance(model_raw, str):
        s = model_raw.strip()
        if s.startswith("{"):
            try:
                model_raw = json.loads(s)
            except (ValueError, TypeError):
                return s
        else:
            return s
    if isinstance(model_raw, dict):
        return str(model_raw.get("id") or model_raw.get("modelID") or "")
    return str(model_raw)


def rollup_metrics(
    quests: Iterable,
    *,
    court_root: Optional[Path] = None,
    db_path: Optional[Path | str] = None,
) -> dict:
    """Compute per-Quest metrics for ``quests`` from one read-only kilo.db scan.

    Returns ``{"quests": [metrics, ...], "db_path": str, "db_available": bool}``.
    When kilo.db is missing, log-ledger goad accounting still runs; session
    metrics read all-zero.
    """
    court_root = court_root or store.get_court_root()
    quests = list(quests)
    db = kilo_db_path(db_path)
    conn = _open_ro(db)

    all_sessions: dict[str, dict] = {}
    all_messages: dict[str, dict] = {}
    if conn is not None:
        try:
            all_sessions = _load_sessions(conn)
            all_messages = _message_counts(
                conn,
                (s["id"] for s in all_sessions.values()),
            )
        finally:
            conn.close()

    results = [
        quest_metrics(
            q,
            court_root=court_root,
            all_sessions=all_sessions,
            all_messages=all_messages,
        )
        for q in quests
    ]
    return {
        "quests": results,
        "db_path": str(db),
        "db_available": conn is not None,
    }


def totals(results: Iterable[dict]) -> dict:
    """Aggregate a list of per-Quest metrics into a grand total."""
    t = {
        "quests": 0, "sessions": 0, "cost_usd": 0.0, "wall_clock_seconds": 0.0,
        "active_seconds": 0.0, "turns": 0, "assistant_messages": 0,
        "messages": 0, "goads": 0,
        "tokens": {"input": 0, "output": 0, "reasoning": 0,
                   "cache_read": 0, "cache_write": 0},
    }
    for m in results:
        t["quests"] += 1
        t["sessions"] += m["sessions"]
        t["cost_usd"] += m["cost_usd"]
        t["wall_clock_seconds"] += m["wall_clock_seconds"]
        t["active_seconds"] += m["active_seconds"]
        t["turns"] += m["turns"]
        t["assistant_messages"] += m["assistant_messages"]
        t["messages"] += m["messages"]
        t["goads"] += m["goads"]
        for k in t["tokens"]:
            t["tokens"][k] += m["tokens"].get(k, 0)
    t["cost_usd"] = round(t["cost_usd"], 6)
    t["wall_clock_seconds"] = round(t["wall_clock_seconds"], 3)
    t["active_seconds"] = round(t["active_seconds"], 3)
    return t


def format_wall_clock(seconds: float) -> str:
    """Compact human rendering of a duration in seconds."""
    seconds = float(seconds or 0)
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{minutes:.1f}m"
    hours = minutes / 60.0
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24.0:.1f}d"
