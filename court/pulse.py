"""Compact, human-readable fleet pulse for the remote Pleasance steward.

``court index`` emits a machine-oriented orientation snapshot and
``court nightwatch`` writes a JSONL journal; neither answers the questions an
away Steward actually asks when it wakes up:

  * is the machine running *right now*?
  * what landed since I last looked?
  * what is queued, and what needs a human?

``court pulse`` folds the Night's Watch journal, the quest board, live worker
census, vigil queue, and recent trunk commits into ONE short Markdown file
(plus a JSON twin for machines). It is deliberately a *last known snapshot*:
the header names the generation time and the watch's last tick, so staleness
is visible rather than implied.

The pulse is published to a dedicated read-only branch, ``status/pleasance``,
never to the ``pleasance`` paperwork lane. That keeps a single writer per
branch: the remote steward writes charters/vigils to ``pleasance``; the local
engine writes telemetry to ``status/pleasance``. Nothing on the status branch
is ever merged or picked up.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import blockers
from . import decisions
from . import git_ops
from . import nightwatch
from . import vigils

PULSE_BRANCH = "status/pleasance"
DEFAULT_LIMIT = 15

_TERMINAL = ("DONE", "LANDED", "LAUNCHED", "READY_TO_RAZE")
_LEDGER_RE = re.compile(r"^- \*\*(?P<ts>\d{4}-\d\d-\d\dT[\d:]+Z)\*\* — (?P<text>.+)$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ts(value) -> float:
    """Best-effort epoch seconds from an ISO string / epoch value."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _age(epoch: float, now: Optional[float] = None) -> Optional[int]:
    if not epoch:
        return None
    return max(0, int((now or datetime.now(timezone.utc).timestamp()) - epoch))


def _human_age(seconds: Optional[int]) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _short(qid: str) -> str:
    return qid.split("-", 1)[0] if "-" in qid else qid


def _git(repo: Path, argv: list, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *argv],
                          capture_output=True, text=True, timeout=timeout)


def _branch(repo: Path) -> str:
    res = _git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    return res.stdout.strip() if res.returncode == 0 else ""


def _tip(repo: Path, ref: str) -> str:
    res = _git(repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    return res.stdout.strip() if res.returncode == 0 else ""


def _watch_liveness(repo: Path, state: dict, workers: dict,
                    stale_after: Optional[int] = None) -> dict:
    """Merge the daemon lock with the durable last-tick stamp into a verdict.

    Three shapes matter to an away steward: a live daemon ticking (running), a
    cron/``--once`` driver whose last tick is recent (running via cron), and a
    daemon that is alive but has not written a tick (stalled).
    """
    if not stale_after:
        stale_after = nightwatch.DEFAULT_INTERVAL * 2
    holder: dict = {}
    try:
        holder = json.loads(nightwatch.lock_path(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    pid = holder.get("pid")
    daemon_alive = bool(pid) and nightwatch._pid_alive(pid)
    last_epoch = float(state.get("last_tick_epoch") or 0)
    age = _age(last_epoch)
    busy = int(workers.get("count") or 0) > 0
    if daemon_alive and (age is None or age <= stale_after):
        verdict = "running"
    elif daemon_alive and busy:
        # A tick can legitimately sit inside a long integration suite; live
        # workers are the honest signal that this is progress, not a stall.
        verdict = "busy"
    elif daemon_alive:
        verdict = "stalled"
    elif age is not None and age <= stale_after:
        verdict = "cron"
    else:
        verdict = "stopped"
    return {
        "state": verdict,
        "pid": pid if daemon_alive else None,
        "last_tick": state.get("last_tick") or "",
        "last_tick_age_seconds": age,
        "stale_after_seconds": stale_after,
        "policy": state.get("policy_line") or "",
        "workers": {
            "count": int(workers.get("count") or 0),
            "rss_mb": workers.get("rss_mb") or 0.0,
            "by_role": workers.get("by_role") or {},
        },
    }


def _ledger_events(repo: Path, want: int) -> list[dict]:
    path = Path(repo) / ".court" / "LEDGER.md"
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-2000:]:
        m = _LEDGER_RE.match(line.strip())
        if not m:
            continue
        out.append({"ts": m.group("ts"), "kind": "ledger", "text": m.group("text").strip()})
    return out[-want:]


def _journal_events(journal: list) -> list[dict]:
    """Turn watch ticks/actions into status transitions and operator events."""
    out: list[dict] = []
    prev_statuses: dict[str, str] = {}
    baseline = False
    for record in journal:
        if not isinstance(record, dict):
            continue
        event = record.get("event")
        ts = str(record.get("ts") or "")
        if event == "tick":
            statuses = record.get("statuses") or {}
            for qid, status in sorted(statuses.items()):
                old = prev_statuses.get(qid)
                if old and old != status:
                    out.append({"ts": ts, "kind": "status",
                                "text": f"{_short(qid)}: {old} → {status}"})
                elif baseline and old is None:
                    out.append({"ts": ts, "kind": "status",
                                "text": f"{_short(qid)}: added as {status}"})
            prev_statuses = {k: str(v) for k, v in statuses.items()}
            baseline = True
        elif event == "action":
            action = record.get("action") or {}
            result = record.get("result") or {}
            verb = action.get("kind") or "action"
            targets = ", ".join(_short(q) for q in (action.get("quest_ids") or []))
            reason = (action.get("reason") or "").strip()
            if targets:
                verb = f"{verb} {targets}"
            if action.get("cogship"):
                verb = f"{verb} [{action['cogship']}]"
            mark = "ok" if result.get("ok") else "FAILED"
            text = f"{verb} — {mark}"
            if reason:
                text += f" · {reason}"
            out.append({"ts": ts, "kind": "action", "text": text})
        elif event in ("start", "stop"):
            out.append({"ts": ts, "kind": "watch",
                        "text": "watch started" if event == "start"
                        else f"watch stopped ({record.get('reason') or 'done'})"})
        elif event in ("tick_failed", "snapshot_failed"):
            out.append({"ts": ts, "kind": "error",
                        "text": f"{event.replace('_', ' ')}: {str(record.get('error') or '')[:160]}"})
        elif event == "pickup":
            picked = record.get("picked") or []
            rejected = record.get("rejected")
            if rejected:
                out.append({"ts": ts, "kind": "pickup",
                            "text": f"pleasance pickup REJECTED: {str(rejected)[:160]}"})
            elif picked:
                out.append({"ts": ts, "kind": "pickup",
                            "text": f"pleasance pickup applied {', '.join(picked)}"})
        elif event == "pickup_remedy":
            action = record.get("action") or "none"
            moved = len(record.get("moved") or [])
            restored = len(record.get("restored") or [])
            if record.get("ok"):
                out.append({"ts": ts, "kind": "pickup",
                            "text": f"pickup self-remediation ({action}): "
                                    f"{moved} scratch file(s) quarantined, "
                                    f"{restored} report(s) restored — retrying"})
            else:
                out.append({"ts": ts, "kind": "error",
                            "text": f"pickup remediation declined: "
                                    f"{str(record.get('error') or 'needs a human')[:160]}"})
    return out


def _merge_events(journal_events: list[dict], ledger_events: list[dict],
                  limit: int) -> list[dict]:
    """Newest-first, ledger timestamp wins over journal position for ties."""
    rows = list(journal_events) + list(ledger_events)
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for row in sorted(rows, key=lambda r: (_ts(r.get("ts")), r.get("kind", "")), reverse=True):
        key = (str(row.get("ts") or ""), str(row.get("text") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _plan_notes(repo: Path, snap: dict, state: dict, workers: dict) -> list[dict]:
    """Re-run the deterministic planner so attention text matches the live watch."""
    try:
        recorded = state.get("policy") if isinstance(state.get("policy"), dict) else {}
        kwargs = {k: v for k, v in recorded.items()
                  if hasattr(nightwatch.Policy, k)}
        policy = nightwatch.Policy(**kwargs)
        qmap = nightwatch._quest_map(snap)
        dt = datetime.now(timezone.utc)
        ready, _ = vigils.ready_vigils(repo, qmap, now=dt)
        charter_ready = nightwatch._charter_ready(repo, qmap, now=dt)
        _actions, notes = nightwatch.plan(
            snap, state, policy, workers, now=dt.timestamp(),
            ready_vigils=ready, charter_ready=charter_ready)
        return notes or []
    except Exception:  # noqa: BLE001 — telemetry must never crash the watch
        return []


def _attention(repo: Path, snap: dict, state: dict, workers: dict,
               notes: Optional[list]) -> list[dict]:
    quests = [q for q in snap.get("quests", []) if q.get("kind") != "epic"]
    rows: list[dict] = []

    def add(level: str, quest: str, text: str) -> None:
        item = {"level": level, "quest": _short(quest), "text": text}
        if item not in rows:
            rows.append(item)

    if notes is None:
        notes = _plan_notes(repo, snap, state, workers)
    for note in notes:
        if note.get("level") != "human":
            continue
        add("human", note.get("quest") or "", (note.get("text") or "").strip())

    for q in quests:
        if q.get("pending_audience") and q.get("status") not in _TERMINAL:
            add("human", q["id"], "pending Serf Audience — an open decision needs a ruling")

    convoys = state.get("convoys") or {}
    for cog, conv in sorted(convoys.items()):
        if isinstance(conv, dict) and isinstance(conv.get("stale"), dict):
            stale = conv["stale"]
            add("human", cog, f"stale gatehouse convoy: {stale.get('reason') or 'dead worktree'}")

    pickup = state.get("last_pickup") or {}
    if pickup.get("rejected"):
        # Name the stable class and the streak so 26 identical ticks read as
        # one growing blocker, and attach the mechanical remedy from the 3rd
        # repeat onward. Bounded: the raw string is truncated, not repeated.
        cls = pickup.get("rejection_class")
        streak = pickup.get("streak_count")
        label = "last pickup rejected"
        if cls:
            label += f" [{cls}"
            if isinstance(streak, int) and streak > 1:
                label += f" streak x{streak}"
            label += "]"
        text = f"{label}: {str(pickup['rejected'])[:150]}"
        if pickup.get("remedy"):
            text += f" — remedy: {str(pickup['remedy'])[:180]}"
        add("human", "pleasance", text)

    return rows[:12]


def _queued(repo: Path, quests: list, state: dict) -> dict:
    def rows(statuses: tuple, detail: str) -> list[dict]:
        return [{"id": q["id"], "title": q.get("title") or "", "status": q.get("status"),
                 "detail": detail}
                for q in quests if q.get("status") in statuses]

    active = rows(("TRIBUTE_READY",), "awaiting Master of Coin (court coin)")
    gate = rows(("GATE",), "awaiting Gatekeeper (court collect)")
    dispatch = rows(("PLANNED", "CHARTERED", "DISPATCHED"),
                    "awaiting the Steward's dispatch decision")
    working = rows(("WORKING", "QUESTING"), "in flight")
    held = rows(("HELD",), "awaiting an Audience decision")

    active_vigils = []
    for spec in vigils.load_all(repo, include_finished=False):
        active_vigils.append({
            "id": spec.get("id"),
            "then": spec.get("then"),
            "when": spec.get("when") or [],
            "expires_at": spec.get("expires_at"),
            "fires": len(spec.get("fires") or []),
            "max_fires": spec.get("max_fires"),
        })

    stale = [{"id": cog, "reason": (conv.get("stale") or {}).get("reason") or ""}
             for cog, conv in sorted((state.get("convoys") or {}).items())
             if isinstance(conv, dict) and isinstance(conv.get("stale"), dict)]

    return {
        "tribute_ready": active,
        "gate": gate,
        "awaiting_dispatch": dispatch,
        "working": working,
        "held": held,
        "vigils": active_vigils,
        "stale_convoys": stale,
    }


def _recent_commits(repo: Path, ref: str, limit: int = 8) -> list[dict]:
    try:
        from . import index
        if not _tip(repo, ref):
            return []
        return index.recent_commits(repo, ref=ref, limit=limit)
    except Exception:  # noqa: BLE001
        return []


def build(repo: Path, *, snap: Optional[dict] = None,
          workers: Optional[dict] = None, state: Optional[dict] = None,
          journal: Optional[list] = None, notes: Optional[list] = None,
          limit: int = DEFAULT_LIMIT, stale_after: Optional[int] = None) -> dict:
    """Assemble the pulse payload. All expensive inputs are injectable so the
    Night's Watch can reuse the snapshot/worker census it already paid for."""
    repo = Path(repo)
    state = state if state is not None else nightwatch.load_state(repo)
    workers = workers if workers is not None else nightwatch.live_kilo_workers(repo)
    snap = snap if snap is not None else nightwatch.snapshot(repo)
    journal = journal if journal is not None else nightwatch.read_journal(repo, max_events=5000)

    quests = [q for q in snap.get("quests", []) if q.get("kind") != "epic"]
    by_status: dict[str, int] = {}
    for q in quests:
        status = q.get("status") or "UNKNOWN"
        by_status[status] = by_status.get(status, 0) + 1

    trunk = {
        "castle": _tip(repo, "castle"),
        "main": _tip(repo, "main"),
        "pleasance": _tip(repo, "pleasance"),
        "status": _tip(repo, f"origin/{PULSE_BRANCH}") or _tip(repo, PULSE_BRANCH),
    }
    commit_ref = "castle" if trunk["castle"] else (_branch(repo) or "main")
    events = _merge_events(_journal_events(journal), _ledger_events(repo, limit * 2), limit)

    return {
        "kind": "COURT_PULSE",
        "version": 1,
        "repo": repo.name,
        "generated_at": _now_iso(),
        "read_area": {
            "status_branch": PULSE_BRANCH,
            "paperwork_branch": "pleasance",
            "note": ("Read telemetry (and the decisions awaiting M'Lord) from the "
                     "status branch; send charters, vigils, and decree packets to the "
                     "pleasance branch. castle is local and never published here."),
        },
        "running": {
            "watch": _watch_liveness(repo, state, workers,
                                     stale_after or nightwatch.DEFAULT_INTERVAL * 2),
            "board": {"total": len(quests),
                      "by_status": dict(sorted(by_status.items(),
                                               key=lambda kv: -kv[1]))},
            # Counts only, from the snapshot/state already in hand: the pulse
            # never pays a second git pass for the blocker board.
            "blockers": blockers.summary_from_snapshot(snap, state),
            "pleasance": state.get("last_pickup") or {},
            "pleasance_remedy": state.get("pickup_remedy") or {},
            "trunk": trunk,
        },
        "events": events,
        "landed_commits": _recent_commits(repo, commit_ref, limit=8),
        "queued": _queued(repo, quests, state),
        "attention": _attention(repo, snap, state, workers, notes),
        "decisions": decisions.collect(repo, snap=snap),
    }


_WATCH_LABEL = {
    "running": "RUNNING",
    "cron": "RUNNING (cron/--once ticks)",
    "busy": "RUNNING (long step; live workers)",
    "stalled": "STALLED (daemon alive, no recent tick)",
    "stopped": "STOPPED (no recent tick)",
}


def render_markdown(pulse: dict) -> str:
    run = pulse.get("running") or {}
    watch = run.get("watch") or {}
    board = run.get("board") or {}
    trunk = run.get("trunk") or {}
    workers = watch.get("workers") or {}
    lines = [
        f"# Court Pulse — {pulse.get('repo')}",
        "",
        f"**Generated** `{pulse.get('generated_at')}` · "
        f"**watch** {_WATCH_LABEL.get(watch.get('state'), 'UNKNOWN')} · "
        f"**last tick** {_human_age(watch.get('last_tick_age_seconds'))}",
        "",
        "> Read-only telemetry published by the Night's Watch on the "
        f"`{pulse.get('read_area', {}).get('status_branch') or PULSE_BRANCH}` branch. "
        "Do not commit work here; send charters and vigils to the `pleasance` branch.",
        "",
        "## Is the machine running?",
        "",
        f"- **Night's Watch:** {_WATCH_LABEL.get(watch.get('state'), 'UNKNOWN')}"
        + (f" — pid {watch['pid']}" if watch.get("pid") else "")
        + (f", last tick `{watch.get('last_tick')}` ({_human_age(watch.get('last_tick_age_seconds'))})"
           if watch.get("last_tick") else ", no tick recorded"),
        f"- **Live workers:** {workers.get('count', 0)} "
        f"({', '.join(f'{k} {v}' for k, v in (workers.get('by_role') or {}).items()) or 'none'})"
        f" · {workers.get('rss_mb', 0.0)} MB RSS",
        f"- **Trunk tips:** castle `{(trunk.get('castle') or '—')[:12]}` · "
        f"main `{(trunk.get('main') or '—')[:12]}` · "
        f"pleasance `{(trunk.get('pleasance') or '—')[:12]}`",
    ]
    pickup = run.get("pleasance") or {}
    if pickup:
        picked = len(pickup.get("picked") or [])
        if pickup.get("rejected"):
            verdict = f"REJECTED — {str(pickup['rejected'])[:160]}"
        elif picked:
            verdict = f"applied {picked} file(s)"
        else:
            verdict = "no-op (nothing new)"
        skipped = pickup.get("skipped_files") or []
        if skipped:
            verdict += f" · {len(skipped)} file(s) skipped"
        lines.append(f"- **Pleasance pickup:** {verdict} "
                     f"({pickup.get('ts') or 'not seen yet'})")
        for item in skipped[:3]:
            lines.append(f"  - skipped `{item.get('path')}` — {item.get('reason')}")
    remedy = run.get("pleasance_remedy") or {}
    if remedy:
        cls = remedy.get("class") or "unknown"
        if remedy.get("ok"):
            lines.append(f"- **Pickup self-remediation:** cleared `{cls}` "
                         f"({remedy.get('action') or 'none'}) — quarantine at "
                         f"`{remedy.get('quarantine_dir') or 'outside the tree'}`")
        else:
            lines.append(f"- **Pickup self-remediation:** could not clear `{cls}` — "
                         f"{str(remedy.get('error') or 'needs a human')[:160]}")
    blockers_summary = run.get("blockers") or {}
    lines += [
        f"- **Board:** {board.get('total', 0)} active — "
        + (" · ".join(f"{k} {v}" for k, v in (board.get('by_status') or {}).items()) or "empty"),
        f"- **Machine blockers:** {blockers_summary.get('parked', 0)} parked "
        f"({blockers_summary.get('conflicts', 0)} conflict, "
        f"{blockers_summary.get('drift', 0)} drift, "
        f"{blockers_summary.get('decisions', 0)} decision) · "
        f"`court blockers` for the full board",
        "",
        "## What landed (last meaningful events)",
        "",
    ]
    events = pulse.get("events") or []
    if events:
        for ev in events:
            stamp = str(ev.get("ts") or "")
            stamp = stamp[11:19] + "Z" if len(stamp) >= 19 else stamp
            lines.append(f"- `{stamp}` {ev.get('text')}")
    else:
        lines.append("- (no events in the watch journal yet)")

    commits = pulse.get("landed_commits") or []
    lines += ["", "## Recent commits (what landed on the trunk)", ""]
    if commits:
        for c in commits[:8]:
            lines.append(f"- `{str(c.get('sha') or '')[:10]}` {c.get('subject')}")
    else:
        lines.append("- (none)")

    queued = pulse.get("queued") or {}
    lines += ["", "## Queued / in flight", ""]

    def section(title: str, rows: list, empty: str = "none") -> None:
        lines.append(f"**{title}** — {len(rows)}")
        if not rows:
            lines.append(f"- {empty}")
        for row in rows[:8]:
            qid = row.get("id") if isinstance(row, dict) else str(row)
            detail = row.get("detail") if isinstance(row, dict) else ""
            title_text = (row.get("title") or "") if isinstance(row, dict) else ""
            suffix = f" — {detail}" if detail else ""
            if title_text:
                suffix += f" · {title_text[:60]}"
            lines.append(f"- `{_short(str(qid))}`{suffix}")
        if len(rows) > 8:
            lines.append(f"- … +{len(rows) - 8} more")
        lines.append("")

    section("TRIBUTE_READY", queued.get("tribute_ready") or [])
    section("GATE", queued.get("gate") or [])
    section("Awaiting dispatch", queued.get("awaiting_dispatch") or [])
    section("Working", queued.get("working") or [])
    if queued.get("held"):
        section("HELD", queued["held"])

    active_vigils = queued.get("vigils") or []
    lines.append(f"**Active vigils** — {len(active_vigils)}")
    if not active_vigils:
        lines.append("- none")
    for spec in active_vigils[:8]:
        lines.append(f"- `{spec.get('id')}` → `{spec.get('then')}` "
                     f"(when {', '.join(spec.get('when') or [])}; expires {spec.get('expires_at')})")
    lines.append("")

    lines += ["## Needs attention", ""]
    attention = pulse.get("attention") or []
    if not attention:
        lines.append("- nothing needs a human right now")
    for item in attention:
        qid = item.get("quest") or ""
        prefix = f"`{qid}` — " if qid else ""
        lines.append(f"- {prefix}{item.get('text')}")

    lines += ["", "## Decisions awaiting M'Lord", ""]
    pending_decisions = pulse.get("decisions") or []
    if not pending_decisions:
        lines.append("- none — nothing needs a royal ruling right now")
    for index, item in enumerate(pending_decisions[:8], start=1):
        qid = item.get("id") or ""
        lines.append(f"{index}. `{_short(qid)}` **{item.get('title') or ''}** "
                     f"— {item.get('kind')} [{item.get('status')}]")
        question = (item.get("question") or "").strip()
        if question:
            lines.append(f"   - {question[:280]}")
        lines.append(f"   - when ruled: {item.get('next') or ''}")
    if len(pending_decisions) > 8:
        lines.append(f"- … +{len(pending_decisions) - 8} more (see PULSE.json)")
    lines.append("")

    lines += [
        "",
        "## Sending a command",
        "",
        "Commands are paperwork commits on the `pleasance` branch; the Night's Watch "
        "runs `court pickup` each tick and applies the valid batch onto `castle`.",
        "",
        "- **New work:** add `.court/quests/<id>.md` with `status: CHARTERED` and a "
        "`## Prerequisites` section (empty = dispatch immediately).",
        "- **One-shot action:** add `.court/vigils/<id>.json`. Allowed actions: "
        "`dispatch:<quest>`, `goad:<quest>`, `coin:<quest>`, `collect[:<cogship>]`, "
        "`advance:<quest>=<STATUS>`, `note:<text>`. Every vigil needs `reason`, "
        "`when`, `then`, `expires_at` (≤7d) and `max_fires` (1..5).",
        "- **Answer a decision:** add `.court/decrees/<id>.json` with "
        "`{\"id\": ..., \"by\": \"M'Lord\", \"decisions\": [{\"quest\": \"Q123-Slug\", "
        "\"ruling\": \"...\", \"status\": \"WORKING\"}]}`. The pickup appends the ruling "
        "to the Quest's Audience Log and applies the safe status; batch several "
        "rulings from one voice session in the same packet.",
        "",
        "\"Landed\" above is the authoritative short list; `PULSE.json` carries the same "
        "payload for tooling.",
        "",
    ]
    return "\n".join(lines)


def local_paths(repo: Path) -> tuple[Path, Path]:
    base = nightwatch.state_dir(repo)
    slug = nightwatch.repo_slug(repo)
    return base / f"{slug}-pulse.md", base / f"{slug}-pulse.json"


def write_local(repo: Path, markdown: str, payload_json: str) -> tuple[Path, Path]:
    md_path, js_path = local_paths(repo)
    try:
        md_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = md_path.with_suffix(".tmp")
        tmp.write_text(markdown, encoding="utf-8")
        tmp.replace(md_path)
        tmp = js_path.with_suffix(".tmp")
        tmp.write_text(payload_json, encoding="utf-8")
        tmp.replace(js_path)
    except OSError:
        pass
    return md_path, js_path


def publish(repo: Path, markdown: str, payload_json: str,
            branch: str = PULSE_BRANCH, remote: str = "origin",
            message: Optional[str] = None) -> dict:
    """Publish the pulse to a dedicated status branch via git plumbing.

    The working tree is never touched (a temporary index is used), so a watch
    tick can publish telemetry without disturbing the castle checkout. The
    branch is single-writer: only the engine commits here, so a push only
    needs to guard against a concurrent publisher, which the retry loop does.
    """
    repo = Path(repo)
    message = message or f"court: pulse {_now_iso()}"
    ref = f"refs/heads/{branch}"
    last_error = ""
    for _attempt in range(3):
        _git(repo, ["fetch", remote, branch], timeout=60)
        tip = _tip(repo, f"{remote}/{branch}") or ""
        if tip:
            existing_md = _git(repo, ["show", f"{tip}:PULSE.md"]).stdout
            existing_js = _git(repo, ["show", f"{tip}:PULSE.json"]).stdout
            if existing_md == markdown and existing_js == payload_json:
                return {"published": False, "reason": "unchanged", "branch": branch,
                        "commit": tip}

        env = {**os.environ}
        with tempfile.TemporaryDirectory() as td:
            index_path = Path(td) / "index"
            env["GIT_INDEX_FILE"] = str(index_path)
            # A disposable two-file mirror: no parent, no trunk tree. This
            # keeps the branch at exactly one commit instead of accumulating a
            # heartbeat commit every tick, and makes the force-push idempotent.
            # NOTE: this must use the temporary index too; `read-tree` against
            # the real index would clear the user's staging area.
            read = subprocess.run(
                ["git", "-C", str(repo), "read-tree", "--empty"],
                capture_output=True, text=True, env=env, timeout=30,
            )
            if read.returncode != 0:
                last_error = (read.stderr or read.stdout).strip()[:300]
                continue

            def blob(text: str) -> str:
                res = subprocess.run(
                    ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                    input=text, capture_output=True, text=True, timeout=30,
                )
                return res.stdout.strip() if res.returncode == 0 else ""

            md_blob = blob(markdown)
            js_blob = blob(payload_json)
            if not md_blob or not js_blob:
                last_error = "hash-object failed"
                continue
            for name, sha in (("PULSE.md", md_blob), ("PULSE.json", js_blob)):
                upd = subprocess.run(
                    ["git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
                     f"100644,{sha},{name}"],
                    capture_output=True, text=True, env=env, timeout=30,
                )
                if upd.returncode != 0:
                    last_error = (upd.stderr or upd.stdout).strip()[:300]
                    break
            else:
                tree = subprocess.run(
                    ["git", "-C", str(repo), "write-tree"],
                    capture_output=True, text=True, env=env, timeout=30,
                )
                if tree.returncode != 0:
                    last_error = (tree.stderr or tree.stdout).strip()[:300]
                    continue
                commit_argv = ["git", "-C", str(repo), "commit-tree",
                               tree.stdout.strip()]
                commit_argv += ["-m", message]
                commit = subprocess.run(commit_argv, capture_output=True, text=True,
                                        timeout=30)
                if commit.returncode != 0:
                    last_error = (commit.stderr or commit.stdout).strip()[:300]
                    continue
                sha = commit.stdout.strip()
                push = _git(repo, ["push", "--force", remote, f"{sha}:{ref}"], timeout=90)
                if push.returncode == 0:
                    _git(repo, ["update-ref", ref, sha])
                    return {"published": True, "reason": "", "branch": branch,
                            "commit": sha}
                last_error = (push.stderr or push.stdout).strip()[:300]
    return {"published": False, "reason": last_error or "publish failed",
            "branch": branch, "commit": ""}


def cmd_pulse(args) -> None:
    repo = Path(getattr(args, "repo", None) or git_ops.get_repo_root()).resolve()
    limit = max(1, int(getattr(args, "limit", DEFAULT_LIMIT) or DEFAULT_LIMIT))
    payload = build(repo, limit=limit)
    markdown = render_markdown(payload)
    payload_json = json.dumps(payload, indent=2) + "\n"
    write_local(repo, markdown, payload_json)
    if getattr(args, "json", False):
        print(payload_json)
    else:
        print(markdown)
    if getattr(args, "publish", False):
        branch = getattr(args, "branch", None) or PULSE_BRANCH
        result = publish(repo, markdown, payload_json, branch=branch)
        if result.get("published"):
            print(f"pulse: published {branch} at {result['commit'][:12]}", file=sys.stderr)
        else:
            print(f"pulse: not published — {result.get('reason')}", file=sys.stderr)
            sys.exit(1)
