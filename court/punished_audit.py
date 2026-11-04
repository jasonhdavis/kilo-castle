"""Deterministic successor audit for PUNISHED quests (`court punished-audit`).

Reads the real charters under ``.court/quests/*.md`` (never assumptions):
for every PUNISHED Quest it reads the ``Judgement of the Condemned`` section
and the ``pilloried_by`` / ``pillory_of`` frontmatter links, then flags
(a) punished quests with NO chartered successor and (b) successors that are
chartered but not advanced. Report-only: invents no work and creates no
quests.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import git_ops
from . import store
from .models import status_label

_ADVANCED_STATUSES = ("DISPATCHED", "QUESTING", "WORKING", "TRIBUTE_READY",
                      "GATE", "READY_TO_RAZE", "LANDED", "LAUNCHED", "DONE")


def _judgement_field(quest, field: str) -> str:
    body = (quest.body_sections or {}).get("Judgement of the Condemned") or ""
    for line in body.splitlines():
        line = line.strip()
        if line.startswith(f"- **{field}:**"):
            value = line.split(":", 1)[1].strip().strip("*").strip()
            if value.lower().startswith("(pending"):
                return ""
            return value.rstrip(")").strip()
    return ""


def _read_reason(quest) -> str:
    body = (quest.body_sections or {}).get("Judgement of the Condemned") or ""
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("- **Reason:**"):
            _prefix, _sep, value = line.partition(":")
            return value.strip()
    return "(no reason recorded in Judgement of the Condemned)"


def audit(root: Path) -> dict:
    quests = store.list_all(include_archive=False, court_root=root / ".court")
    by_id = {q.id: q for q in quests}

    # successor -> predecessor links from ALL quests (the successor may live in
    # any status, and the two fields must stay linked via `court charter
    # --pillory-of`).
    pillory_of_index: dict[str, str] = {
        q.id: q.pilloried_by for q in quests if (q.pilloried_by or "").strip()
    }
    for q in quests:
        po = (getattr(q, "pillory_of", "") or "").strip()
        if po:
            pillory_of_index.setdefault(po, q.id)

    rows: list[dict] = []
    for q in quests:
        if q.status != "PUNISHED":
            continue
        frontmatter_succ = (q.pilloried_by or "").strip()
        judgement_succ = _judgement_field(q, "Successor Quest")
        successor = frontmatter_succ or judgement_succ or pillory_of_index.get(q.id, "")
        if successor.lower().startswith("(pending"):
            successor = ""
        succ_obj = None
        succ_status = ""
        succ_archived = False
        if successor:
            try:
                succ_obj = store.load(successor, court_root=root / ".court")
            except Exception:  # noqa: BLE001 — a dangling id must not kill the audit
                succ_obj = None
            if succ_obj is not None:
                succ_status = succ_obj.status
                succ_archived = store.find_path(successor, court_root=root / ".court").parent.name == "archive"
        missing = not bool(successor)
        # Archived successors have ran their course: treat as advanced.
        not_advanced = bool(successor) and not succ_archived and succ_status not in _ADVANCED_STATUSES
        also_punished = succ_status == "PUNISHED"
        rows.append({
            "id": q.id,
            "title": q.title,
            "section": q.section,
            "reason": _read_reason(q),
            "successor": successor,
            "successor_status": succ_status,
            "successor_archived": succ_archived,
            "successor_title": succ_obj.title if succ_obj else "",
            "links": {"frontmatter": frontmatter_succ, "judgement": judgement_succ},
            "no_successor": missing,
            "successor_not_advanced": not_advanced,
            "successor_also_punished": also_punished,
            "branch": q.branch or "",
        })

    no_succ = [r for r in rows if r["no_successor"]]
    not_adv = [r for r in rows if r["successor_not_advanced"]]
    chains = [r for r in rows if r["successor_also_punished"]]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "repo": str(root),
        "punished_total": len(rows),
        "with_successor": len(rows) - len(no_succ),
        "no_successor": len(no_succ),
        "successor_not_advanced": len(not_adv),
        "successor_also_punished": len(chains),
        "flags": {
            "no_successor": [r["id"] for r in no_succ],
            "successor_not_advanced": [
                {"punished": r["id"], "successor": r["successor"], "successor_status": r["successor_status"]}
                for r in not_adv
            ],
            "successor_also_punished": [
                {"punished": r["id"], "successor": r["successor"]} for r in chains
            ],
        },
        "rows": rows,
    }


def render_report(data: dict) -> str:
    repo = Path(data.get("repo") or "")
    lines = [
        "# PUNISHED Quest — Successor Audit",
        "",
        f"Generated: {data['generated_at']} · repo: `{repo.name or data.get('repo')}`",
        "",
        "> Deterministic read of `.court/quests/*.md` charters (Judgement of the "
        "Condemned + `pilloried_by`/`pillory_of` links). Report only — no quests "
        "created or modified.",
        "",
        "## Counts",
        "",
        f"- Punished quests: **{data['punished_total']}**",
        f"- With a chartered successor: **{data['with_successor']}**",
        f"- **No successor (flag a): {data['no_successor']}**",
        f"- Successor chartered but NOT advanced (flag b): **{data['successor_not_advanced']}**",
        f"- Successor is itself PUNISHED (chain): **{data['successor_also_punished']}**",
        "",
        "## Flag (a) — punished with NO successor",
        "",
    ]
    flag_a = data["flags"]["no_successor"]
    if flag_a:
        for qid in flag_a:
            lines.append(f"- {qid}")
    else:
        lines.append("_none_")
    lines += ["", "## Flag (b) — successor chartered but not advanced", ""]
    flag_b = data["flags"]["successor_not_advanced"]
    if flag_b:
        for row in flag_b:
            lines.append(f"- `{row['punished']}` → `{row['successor']}` [{row['successor_status']}]")
    else:
        lines.append("_none_")
    lines += ["", "## Chains — successor is itself PUNISHED", ""]
    chains = data["flags"]["successor_also_punished"]
    if chains:
        for row in chains:
            lines.append(f"- `{row['punished']}` → `{row['successor']}` (also PUNISHED)")
    else:
        lines.append("_none_")
    lines += ["", "## Per-quest detail", ""]
    for r in data["rows"]:
        succ = r["successor"] or "(none)"
        succ_info = f"{r['successor_status']}" + (" (archived)" if r.get("successor_archived") else "")
        if r.get("successor_title"):
            succ_info += f" — {r['successor_title'][:48]}"
        lines.append(f"### {r['id']} — {r['title'][:72]}")
        lines.append(f"- status: {status_label('PUNISHED')} · section: {r['section'] or '-'}")
        lines.append(f"- successor: `{succ}` {succ_info}")
        lines.append(f"- links: frontmatter={r['links']['frontmatter'] or '(none)'}; "
                     f"judgement={r['links']['judgement'] or '(none)'}")
        lines.append(f"- flags: no_successor={r['no_successor']} · not_advanced={r['successor_not_advanced']} "
                     f"· also_punished={r['successor_also_punished']}")
        lines.append(f"- reason: {r['reason'][:220]}")
        lines.append("")
    return "\n".join(lines)


def cmd_punished_audit(args) -> None:
    repo = Path(getattr(args, "repo", None) or git_ops.get_repo_root()).resolve()
    data = audit(repo)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
        return
    print(f"PUNISHED audit: {data['punished_total']} punished · "
          f"{data['no_successor']} with NO successor · "
          f"{data['successor_not_advanced']} successors chartered but not advanced · "
          f"{data['successor_also_punished']} chains")
    out_dir = Path(getattr(args, "out", None) or (repo / "docs")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "PUNISHED_AUDIT.md"
    out_path.write_text(render_report(data), encoding="utf-8")
    print(f"wrote {out_path}")