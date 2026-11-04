"""Deterministic repository-intelligence snapshot (`court index`).

Emits a compact AI-orienting index (Markdown + JSON) that an external agent
reads BEFORE exploring the repo. Generated entirely from deterministic git
and ``.court`` records — never from a summarizer. The artifact is a LAST
KNOWN SNAPSHOT: it carries its own ``generated_at`` plus the
``main``/``castle``/``pleasance`` tips and the ``castle`` HEAD SHA it was
generated from, and is clearly labelled as such; the live runtime state is
``court status --json`` (or ``court schedule`` for per-quest eligibility).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import git_ops
from . import nightwatch
from . import store
from .models import status_label

# Board-snapshot churn the pipeline writes every tick; not "meaningful" commit
# history for an orienting index.
_SKIP_COMMIT_RE = re.compile(r"^court: (board snapshot|update)", re.IGNORECASE)

# Static orientation map: where a newcomer finds each subsystem. Points at
# paths only; source content is never embedded in the artifact.
ARCHITECTURE = [
    ("Django entry point", "manage.py"),
    ("Django apps", "apps/ (the repo's packaged Django application modules)"),
    ("Celery tasks", "tasks/"),
    ("Templates", "templates/"),
    ("Static assets", "static/"),
    ("Tests", "tests/"),
    ("Operational scripts", "scripts/"),
    ("Court pipeline engine", "court/ (quest/ledger ledger, nightwatch supervisor, pleasance pickup)"),
    ("Quest charters & ledger", ".court/quests/, .court/epics/, .court/LEDGER.md"),
    ("Remote-steward lane contract", "PLEASANCE.md"),
    ("Branch-specific agent guidance", "AGENTS.md (castle = internal ops; pleasance = external agents)"),
    ("Runbooks & deep docs", "docs/"),
]

TERMINAL_STATUSES = ("DONE", "LANDED", "LAUNCHED", "READY_TO_RAZE")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _rel_to_root(path: str, root: Path) -> str:
    """Relativize a worktree path under the repo root so the committed
    artifact never leaks the developer's absolute machine layout."""
    try:
        p = Path(path).resolve()
        r = root.resolve()
        if p == r:
            return "."
        return p.relative_to(r).as_posix()
    except (ValueError, OSError):
        return path


def branch_tip(root: Path, ref: str) -> str:
    res = git_ops._run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], root)
    if not (res.get("ok") and (res.get("stdout") or "").strip()):
        return ""
    return res["stdout"].strip()


def _count_range(root: Path, a: str, b: str) -> int:
    res = git_ops._run(["git", "rev-list", "--count", f"{a}..{b}"], root)
    if not (res.get("ok") and (res.get("stdout") or "").strip()):
        return 0
    try:
        return int(res["stdout"].strip())
    except ValueError:
        return 0


def recent_commits(root: Path, ref: str = "castle", limit: int = 20) -> list[dict]:
    res = git_ops._run(
        ["git", "log", f"{ref}", "--oneline", "--no-merges", "-n", str(limit * 2)], root)
    out: list[dict] = []
    for line in (res.get("stdout") or "").splitlines():
        parts = line.split(" ", 1)
        if len(parts) < 2:
            continue
        sha, subject = parts[0], parts[1].strip()
        if _SKIP_COMMIT_RE.match(subject):
            continue
        out.append({"sha": sha, "subject": subject[:100]})
        if len(out) >= limit:
            break
    return out


def _judgement_successor(quest) -> str:
    """Read the Judgement of the Condemned section for the named successor,
    independent of the frontmatter field (older charters only wrote it there)."""
    body = (quest.body_sections or {}).get("Judgement of the Condemned") or ""
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("- **Successor Quest:**"):
            value = line.split(":", 1)[1].strip().strip("*").strip()
            if value.lower().startswith("(pending") or value.startswith("—"):
                return ""
            return value
    return ""


def punished_entries(root: Path) -> list[dict]:
    quests = store.list_all(include_archive=False, court_root=root / ".court")
    rev: dict[str, str] = {}
    for q in quests:
        po = (q.pilloried_by or "").strip()
        if not po:
            po = _judgement_successor(q)
        if po:
            rev.setdefault(q.id, po)
    out: list[dict] = []
    for q in quests:
        if q.status != "PUNISHED":
            continue
        succ = (q.pilloried_by or "").strip() or rev.get(q.id, "")
        succ_status = ""
        succ_archived = False
        if succ:
            try:
                succ_obj = store.load(succ, court_root=root / ".court")
                succ_status = succ_obj.status
                succ_archived = store.find_path(succ, court_root=root / ".court").parent.name == "archive"
            except Exception:  # noqa: BLE001 — a dangling id must not kill the index
                succ_status = "(dangling id)"
        out.append({"id": q.id, "successor": succ, "successor_status": succ_status,
                    "successor_archived": succ_archived})
    return out


def build_snapshot(root: Path) -> dict:
    quests = store.list_all(include_archive=False, court_root=root / ".court")
    by_status: dict[str, int] = {}
    by_app: dict[str, int] = {}
    for q in quests:
        by_status[q.status] = by_status.get(q.status, 0) + 1
        app = (q.app or "uncategorized").strip()
        by_app[app] = by_app.get(app, 0) + 1

    def ids(statuses: tuple[str, ...]) -> list[str]:
        return [q.id for q in quests if q.status in statuses]

    castle_head = branch_tip(root, "castle")
    wt_list = git_ops.list_git_worktrees(root, use_cache=False)
    workers = {"count": 0, "rss_mb": 0.0}
    try:
        workers = nightwatch.live_kilo_workers(root)
    except Exception:  # noqa: BLE001 — observation must never kill the index
        pass

    # Scheduling bottlenecks (deterministic read of the board + watch state).
    bottlenecks: list[dict] = []
    tr = ids(("TRIBUTE_READY",))
    if tr:
        bottlenecks.append({"type": "queued_for_master_of_coin", "quests": tr,
                            "detail": "tribute rendered; awaiting MoC audit (court coin)"})
    gate = ids(("GATE",))
    if gate:
        bottlenecks.append({"type": "queued_for_gatekeeper", "quests": gate,
                            "detail": "MoC-approved; awaiting convoy packing (court collect)"})
    planned_chartered = ids(("PLANNED", "CHARTERED", "DISPATCHED"))
    if planned_chartered:
        bottlenecks.append({"type": "awaiting_dispatch_decision", "quests": planned_chartered[:12],
                            "detail": f"{len(planned_chartered)} quest(s) awaiting a Steward dispatch decision"})
    try:
        watch = nightwatch.load_state(root)
        convoys = watch.get("convoys") or {}
        stale = [k for k, v in convoys.items()
                 if isinstance(v, dict) and isinstance(v.get("stale"), dict)]
        if stale:
            bottlenecks.append({"type": "stale_convoy", "quests": stale,
                                "detail": "dead/stale gatehouse worktree blocking a convoy"})
    except Exception:  # noqa: BLE001
        pass
    punished = punished_entries(root)
    no_succ = [p for p in punished if not p["successor"]]
    if no_succ:
        bottlenecks.append({"type": "punished_without_successor",
                            "quests": [p["id"] for p in no_succ],
                            "detail": "frozen side-state with no successor chartered"})

    completed = [
        {"id": q.id, "status": q.status, "razed_at": getattr(q, "razed_at", "") or ""}
        for q in quests if q.status in TERMINAL_STATUSES
    ]
    completed.sort(key=lambda r: r["razed_at"], reverse=True)

    return {
        "kind": "AI_INDEX",
        "version": 1,
        "repo_name": root.name,
        "generated_at": _now_iso(),
        "freshness": {
            "note": ("last known snapshot — NOT live runtime state. "
                     "Regenerate with `python3 -m court.cli index`; "
                     "live state: `python3 -m court.cli status --json`."),
            "castle_head": castle_head,
            "main_tip": branch_tip(root, "main"),
            "pleasance_tip": branch_tip(root, "pleasance"),
            "castle_behind_main": _count_range(root, "castle", "main"),
            "main_behind_castle": _count_range(root, "main", "castle"),
        },
        "architecture": [{"label": label, "path": path} for label, path in ARCHITECTURE],
        "quests": {
            "total": len(quests),
            "by_status": by_status,
            "by_app": dict(sorted(by_app.items(), key=lambda kv: -kv[1])),
            "notable": {
                "tribute_ready": tr,
                "gate": gate,
                "working": ids(("WORKING", "QUESTING")),
                "punished": sorted(ids(("PUNISHED",))),
                "held": ids(("HELD",)),
                "open": ids(("OPEN",)),
            },
            "completed": completed[:15],
            "completed_total": len(completed),
        },
        "worktrees": [
            {"path": (_rel_to_root(w.get("worktree", ""), root) or w.get("worktree", "")),
             "branch": w.get("branch", "") or "(detached)",
             "head": (w.get("head") or "")[:12]}
            for w in wt_list
        ],
        "live_workers": {"count": workers.get("count", 0),
                         "rss_mb": workers.get("rss_mb", 0.0),
                         "by_role": workers.get("by_role") or {}},
        "recent_commits": recent_commits(root)[:20],
        "punished": punished,
        "bottlenecks": bottlenecks,
        "orientation": {
            "read_first": ["PLEASANCE.md", "AGENTS.md (pleasance branch)", "docs/AI_INDEX.md"],
            "board": "court status --json",
            "new_quest_flow": "charter via .court/quests/<id>.md on the pleasance branch; see PLEASANCE.md",
        },
    }


def render_markdown(snap: dict) -> str:
    f = snap["freshness"]
    q = snap["quests"]
    lines = [
        f"# AI Index — {snap['repo_name']}",
        "",
        "> Deterministic orientation for AI agents. **Last known snapshot** — not live state.",
        "",
        "## Freshness",
        "",
        f"- generated_at: `{snap['generated_at']}` (UTC)",
        f"- `castle` HEAD: `{f['castle_head'][:12]}` (`{f['castle_head']}`)",
        f"- `main` tip: `{f['main_tip'][:12]}` · `pleasance` tip: `{f['pleasance_tip'][:12]}` · "
        f"castle ahead of main: {f['castle_behind_main']} · main ahead of castle: {f['main_behind_castle']}",
        f"- {f['note']}",
        "",
        "## Repo layout (entry points)",
        "",
    ]
    for entry in snap["architecture"]:
        lines.append(f"- **{entry['label']}:** `{entry['path']}`")
    lines += [
        "",
        "## Quest board",
        "",
        f"Active quest records: **{q['total']}**.",
        "",
        "| status | count |",
        "|---|---|",
    ]
    for status in sorted(q["by_status"], key=lambda s: -q["by_status"][s]):
        lines.append(f"| {status_label(status)} (`{status}`) | {q['by_status'][status]} |")
    lines.append("")
    lines.append("Top apps by quest count: "
                 + ", ".join(f"{app} ({n})" for app, n in list(q["by_app"].items())[:8]) + ".")
    lines += ["", "### Notable quests"]

    def bullet(label: str, ids_: list[str], empty: str) -> str:
        if ids_:
            return f"- **{label}** ({len(ids_)}): {', '.join(ids_)}"
        return f"- **{label}**: {empty}"

    notable = q["notable"]
    lines.append(bullet("TRIBUTE_READY (awaiting Master of Coin)", notable["tribute_ready"], "none"))
    lines.append(bullet("GATE (awaiting Gatekeeper)", notable["gate"], "none"))
    lines.append(bullet("WORKING (serfs in the field)", notable["working"], "none"))
    lines.append(bullet("PUNISHED (frozen side-state)", notable["punished"], "none"))
    lines.append(bullet("HELD (awaiting Audience)", notable["held"], "none"))
    lines.append(bullet("OPEN (unscoped backlog)", notable["open"], "none"))
    lines.append(f"- **Recently completed** ({q['completed_total']} terminal): "
                 + ", ".join(f"{c['id'][:24]}" for c in q["completed"][:10]) or "none")
    lines += ["", "## Worktrees & live workers", ""]
    ws = snap["worktrees"]
    lines.append(f"`{len(ws)}` registered git worktrees; "
                 f"**{snap['live_workers']['count']} live kilo worker(s)** "
                 f"({snap['live_workers']['rss_mb']} MB RSS, roles "
                 f"{snap['live_workers']['by_role'] or 'none'}).")
    for w in ws[:12]:
        lines.append(f"- `{w['path']}` → `{w['branch']}` ({w['head']})")
    lines += ["", "## Recent meaningful commits (castle)", ""]
    for c in snap["recent_commits"][:12]:
        lines.append(f"- `{c['sha'][:10]}` {c['subject']}")
    lines += ["", "## Punished quests & successors", ""]
    if snap["punished"]:
        for p in snap["punished"]:
            succ = p["successor"] or "— NONE —"
            tag = " (archived)" if p.get("successor_archived") and p["successor"] else ""
            lines.append(f"- `{p['id']}` → successor `{succ}` ({p['successor_status'] or 'n/a'}{tag})")
    else:
        lines.append("- none")
    lines += ["", "## Scheduling bottlenecks", ""]
    if snap["bottlenecks"]:
        for b in snap["bottlenecks"]:
            lines.append(f"- {b['detail']}: {', '.join(b['quests'][:8])}"
                         + (f" (+{len(b['quests']) - 8})" if len(b["quests"]) > 8 else ""))
    else:
        lines.append("- none detected")
    lines += ["", "## Orientation", ""]
    lines.append("- Read first: `PLEASANCE.md`, `AGENTS.md` (pleasance branch), this file.")
    lines.append("- Live board: `python3 -m court.cli status --json`.")
    lines.append("- New quest flow: author `.court/quests/<id>.md` on the `pleasance` branch; see `PLEASANCE.md`.")
    return "\n".join(lines) + "\n"


def cmd_index(args) -> None:
    repo = Path(getattr(args, "repo", None) or git_ops.get_repo_root()).resolve()
    snap = build_snapshot(repo)
    out_dir = Path(getattr(args, "out", None) or (repo / "docs")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "AI_INDEX.md"
    js_path = out_dir / "AI_INDEX.json"
    md_path.write_text(render_markdown(snap), encoding="utf-8")
    js_path.write_text(json.dumps(snap, indent=2) + "\n", encoding="utf-8")
    print(f"index: wrote {md_path} and {js_path} (generated_at {snap['generated_at']}, "
          f"castle {snap['freshness']['castle_head'][:12]})")