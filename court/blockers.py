"""Deterministic blocker board for the Court (`court blockers`).

A Steward waking up needs one answer: *what is parked on this machine, and
what single verb clears each item?* The Night's Watch acts on those blockers
mechanically but only exposes them as journal lines; `court status` shows the
board without naming the next verb. This module joins the two, with no model
calls and no side effects.

A row is emitted for every non-terminal Quest (epics excluded — their children
carry the work). Classification is ordered by severity:

  conflict > drift > decision > no-worktree / worker-stalled > ready

READY_TO_RAZE is the exception: the branch is already merged/promoted and the
worktree is about to be torn down, so it is always `ready` with `court raze`
as its only verb — drift there is noise, not a blocker.

`next` is exactly one mechanical or human verb. Be honest about ambiguity: a
Quest with both drift and a pending Audience reports drift first (the
mechanical rebase is the precondition for the coin), and the audience still
appears in the pulse's decision frontier.

Performance: on a 200-Quest repo only Quests in a non-terminal status that
also have an existing worktree pay a `git rev-list --left-right --count` call;
everything else is in-memory. Worktree discovery reuses git_ops' cached
`git worktree list`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable, Optional

from . import git_ops
from . import nightwatch
from . import store

# Terminal for board purposes: the Quest is shipped or already promoted onto
# the trunk. READY_TO_RAZE stays in the board (it still needs `court raze`).
TERMINAL_STATUSES = ("DONE", "LANDED", "LAUNCHED")

# Statuses whose Quest cannot progress without a live worktree.
WORKTREE_STATUSES = ("QUESTING", "WORKING", "TRIBUTE_READY", "GATE", "READY_TO_RAZE")

# Severity order for sorting (lower sorts first).
_BLOCKER_RANK = {
    "conflict": 0,
    "drift": 1,
    "decision": 2,
    "no-worktree": 3,
    "worker-stalled": 4,
    "ready": 5,
}


def _short(qid: str) -> str:
    return qid.split("-", 1)[0] if "-" in qid else qid


def _rank(blocker: str) -> int:
    head = (blocker or "").split(":", 1)[0]
    return _BLOCKER_RANK.get(head, 3)


def _ahead_behind(worktree: Path | str,
                  base: str = "castle") -> tuple[Optional[int], Optional[int]]:
    """``(ahead, behind)`` versus the base trunk; ``(None, None)`` on failure."""
    try:
        res = subprocess.run(
            ["git", "-C", str(worktree), "rev-list", "--left-right", "--count",
             f"{base}...HEAD"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    parts = (res.stdout or "").split() if res.returncode == 0 else []
    if len(parts) != 2:
        return None, None
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return None, None
    return ahead, behind


def _worker(worktree: Optional[Path | str]) -> str:
    if not worktree:
        return "unknown"
    try:
        busy = nightwatch.occupied(str(worktree))
    except Exception:  # noqa: BLE001 — observation must never crash the board
        return "unknown"
    if not busy:
        return "idle"
    return f"{busy.get('role') or 'worker'}:{busy.get('state') or 'unknown'}"


def _next_verb(qid: str, status: str, blocker: str) -> str:
    short = _short(qid)
    if blocker.startswith("conflict") or blocker.startswith("drift"):
        return f"court rebase {short}"
    if blocker == "decision":
        return "await M'Lord ruling"
    if blocker == "worker-stalled":
        return f"court goad {short}"
    if blocker == "no-worktree":
        return "dispatch Serf"
    if status == "TRIBUTE_READY":
        return f"court coin {short}"
    if status == "GATE":
        return "court collect"
    if status == "READY_TO_RAZE":
        return f"court raze {short}"
    if status in ("PLANNED", "CHARTERED", "DISPATCHED"):
        return "dispatch Serf"
    if status in ("OPEN", "HELD"):
        return "await M'Lord ruling"
    return "none"


def classify_row(*, status: str, worktree: Optional[Path | str],
                 behind: Optional[int], pending_audience: bool,
                 conflict_files: Iterable[str], worker: str) -> str:
    """The single blocker class for one Quest (pass either to a row or to a note)."""
    # READY_TO_RAZE is already merged/promoted: its worktree is about to be
    # torn down, so drift/conflict there is noise that would bury the rows
    # that actually need action. The only verb is the raze itself.
    if (status or "").strip().upper() == "READY_TO_RAZE":
        return "ready"
    files = [str(f) for f in (conflict_files or [])]
    if files:
        return "conflict:" + ",".join(files)
    if behind and behind > 0:
        return f"drift:{int(behind)}"
    if pending_audience:
        return "decision"
    if status in WORKTREE_STATUSES and not worktree:
        return "no-worktree"
    if worker.endswith(":stalled"):
        return "worker-stalled"
    return "ready"


def build_row(quest, *, worktree: Optional[Path | str] = None,
              behind: Optional[int] = None, ahead: Optional[int] = None,
              worker: Optional[str] = None, state_entry: Optional[dict] = None) -> dict:
    """Assemble one board row. Pure given its inputs (no git when injected)."""
    entry = state_entry or {}
    conflict = entry.get("merge_conflict") if isinstance(entry, dict) else None
    conflict_files = sorted({str(f) for f in ((conflict or {}).get("files") or [])}) \
        if isinstance(conflict, dict) else []
    pending = bool(getattr(quest, "has_pending_audience", lambda: False)())
    worker_state = worker if worker is not None else _worker(worktree)
    blocker = classify_row(
        status=quest.status, worktree=worktree, behind=behind,
        pending_audience=pending, conflict_files=conflict_files,
        worker=worker_state,
    )
    return {
        "id": quest.id,
        "status": quest.status,
        "app": getattr(quest, "app", "") or "",
        "ahead": ahead,
        "behind": behind,
        "worktree": str(worktree) if worktree else "",
        "worker": worker_state,
        "pending_audience": pending,
        "merge_conflict": conflict_files,
        "blocker": blocker,
        "next": _next_verb(quest.id, quest.status, blocker),
    }


def summarize(rows: list[dict]) -> dict:
    counts = {"conflict": 0, "drift": 0, "decision": 0,
              "no-worktree": 0, "worker-stalled": 0, "ready": 0}
    for row in rows:
        head = (row.get("blocker") or "ready").split(":", 1)[0]
        counts[head if head in counts else "ready"] += 1
    parked = (counts["conflict"] + counts["drift"]
              + counts["no-worktree"] + counts["worker-stalled"])
    return {
        "total": len(rows),
        "parked": parked,
        "conflicts": counts["conflict"],
        "drift": counts["drift"],
        "decisions": counts["decision"],
        "no_worktree": counts["no-worktree"],
        "worker_stalled": counts["worker-stalled"],
        "ready": counts["ready"],
    }


def collect(repo: Path, *, quests=None, state: Optional[dict] = None,
            base: str = "castle") -> dict:
    """Build the full blocker payload for ``repo`` (deterministic, read-only)."""
    repo = Path(repo).resolve()
    quests = quests if quests is not None else store.list_all(
        include_archive=False, court_root=repo / ".court")
    state = state if state is not None else nightwatch.load_state(repo)
    state_quests = state.get("quests") or {}

    rows: list[dict] = []
    for quest in quests:
        if getattr(quest, "kind", "quest") == "epic":
            continue
        if (quest.status or "").strip().upper() in TERMINAL_STATUSES:
            continue
        worktree = None
        try:
            worktree = git_ops.find_worktree_for_quest(quest, cwd=repo)
        except Exception:  # noqa: BLE001 — a broken worktree must not hide the row
            worktree = None
        ahead = behind = None
        if worktree is not None:
            ahead, behind = _ahead_behind(worktree, base=base)
        entry = state_quests.get(quest.id) or {}
        rows.append(build_row(quest, worktree=worktree, behind=behind, ahead=ahead,
                              state_entry=entry))
    rows.sort(key=lambda r: (_rank(r["blocker"]), r["id"]))
    return {"quests": rows, "summary": summarize(rows)}


def summary_from_snapshot(snap: dict, state: Optional[dict], base: str = "castle") -> dict:
    """Cheap counts for the pulse from data it already holds.

    The pulse has `court status --json` (behind is already in `git_status`) and
    the watch state (merge conflicts), so it must not pay extra git calls."""
    state = state or {}
    state_quests = state.get("quests") or {}
    counts = {"conflict": 0, "drift": 0, "decision": 0,
              "no-worktree": 0, "worker-stalled": 0, "ready": 0}
    total = 0
    for quest in snap.get("quests") or []:
        if quest.get("kind") == "epic":
            continue
        status = (quest.get("status") or "").strip().upper()
        if status in TERMINAL_STATUSES:
            continue
        total += 1
        entry = state_quests.get(quest.get("id")) or {}
        conflict = entry.get("merge_conflict") if isinstance(entry, dict) else None
        conflict_files = ((conflict or {}).get("files") or []) \
            if isinstance(conflict, dict) else []
        behind = (quest.get("git_status") or {}).get("behind") or 0
        blocker = classify_row(
            status=status, worktree=quest.get("worktree"),
            behind=behind, pending_audience=bool(quest.get("pending_audience")),
            conflict_files=conflict_files,
            worker=((quest.get("worker") or {}).get("state") or "unknown"),
        )
        counts[blocker.split(":", 1)[0]] += 1
    parked = (counts["conflict"] + counts["drift"]
              + counts["no-worktree"] + counts["worker-stalled"])
    return {
        "total": total,
        "parked": parked,
        "conflicts": counts["conflict"],
        "drift": counts["drift"],
        "decisions": counts["decision"],
        "no_worktree": counts["no-worktree"],
        "worker_stalled": counts["worker-stalled"],
        "ready": counts["ready"],
    }


def render_text(payload: dict) -> str:
    """A compact, aligned table; one line per Quest, worst first."""
    rows = payload.get("quests") or []
    header = ("QUEST", "STATUS", "APP", "BEHIND", "AHEAD", "BLOCKER", "NEXT")
    body: list[tuple[str, ...]] = []
    for row in rows:
        body.append((
            _short(str(row.get("id") or "")),
            str(row.get("status") or ""),
            str(row.get("app") or ""),
            "-" if row.get("behind") is None else str(row.get("behind")),
            "-" if row.get("ahead") is None else str(row.get("ahead")),
            str(row.get("blocker") or ""),
            str(row.get("next") or ""),
        ))
    widths = [len(h) for h in header]
    for line in body:
        for i, cell in enumerate(line):
            widths[i] = min(max(widths[i], len(cell)), 44 if i == 5 else 24)
    def fmt(line: tuple[str, ...]) -> str:
        cells = []
        for i, cell in enumerate(line):
            text = cell if i == len(line) - 1 else cell[:widths[i]]
            cells.append(text.ljust(widths[i]) if i < len(line) - 1 else text)
        return "  ".join(cells).rstrip()
    out = [fmt(header), fmt(tuple("-" * w for w in widths))]
    out.extend(fmt(line) for line in body)
    summary = payload.get("summary") or {}
    out.append("")
    out.append(
        f"{summary.get('total', 0)} non-terminal · {summary.get('parked', 0)} parked "
        f"({summary.get('conflicts', 0)} conflict, {summary.get('drift', 0)} drift, "
        f"{summary.get('decisions', 0)} decision, "
        f"{summary.get('no_worktree', 0)} no-worktree, "
        f"{summary.get('worker_stalled', 0)} stalled) · "
        f"{summary.get('ready', 0)} ready"
    )
    return "\n".join(out)
