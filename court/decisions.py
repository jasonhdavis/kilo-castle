"""Royal decrees: the Pleasance lane's inbound answers to pending Audiences.

The away Steward lane has two halves:

  * the local Court publishes *what needs a ruling* (the decision brief — the
    ``Decisions awaiting M'Lord`` section of ``court pulse`` and the local
    ``court decisions`` command), and
  * the remote voice-chat agent commits *what M'Lord ruled* back up the
    ``pleasance`` branch as a decree packet:
    ``.court/decrees/<id>.json``.

A decree packet is deliberately a BATCH — one file can carry every ruling
from one voice session — because the point is to clear several stalled areas
in a single round trip::

    {
      "id": "decree-2026-11-05-1200",
      "by": "M'Lord",
      "at": "2026-11-05T12:00:00Z",
      "source": "voice via the Pleasance agent",
      "decisions": [
        {"quest": "Q123-Foo", "question": "Which auth model?",
         "ruling": "Use session cookies; no JWT.", "status": "WORKING"}
      ]
    }

``court pickup`` validates the packet, appends one dated block to each named
Quest's ``## Audience Log``, and — when a ruling names a safe target status
(or the Quest is ``HELD`` and the ruling leaves the status implicit) — moves
the Quest out of its royal side-state so the Night's Watch resumes it.

Statuses that are terminal or destructive (``READY_TO_RAZE``, ``DONE``,
``PUNISHED``, ``DEMOTED``) are NOT decree targets: razing and pillorying stay
human pipeline acts, mirroring the vigil action allow-list.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import git_ops
from . import store

DECREE_DIR = "decrees"
DECREE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,60}")
MIN_RULING_CHARS = 3
MAX_DECISIONS = 50
MAX_QUESTION_CHARS = 800

# Target statuses a decree may set. Deliberately excludes the terminal and
# destructive side-states: an away LLM must never raze, ship, or pillory.
SAFE_STATUSES = (
    "OPEN", "PLANNED", "CHARTERED", "DISPATCHED", "QUESTING", "WORKING",
    "TRIBUTE_READY", "GATE", "HELD",
)

_KIND_ORDER = {"blocked": 0, "audience": 1, "ui": 2}
_TERMINAL = ("DONE", "LANDED", "LAUNCHED", "READY_TO_RAZE")


class DecreeError(ValueError):
    """A decree packet that violates the lane's narrowness contract."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def decree_dir(repo: Path) -> Path:
    return Path(repo) / ".court" / DECREE_DIR


def quest_path(repo: Path, quest_id: str) -> Optional[Path]:
    """Resolve a decree's quest id to its charter path (short ids accepted)."""
    return store.find_path(quest_id, court_root=Path(repo) / ".court")


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------


def _parse_iso(value: str) -> Optional[str]:
    try:
        dt = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_decree_packet(text: str) -> dict:
    """Parse and structurally validate one decree packet. Raises DecreeError."""
    try:
        spec = json.loads(text)
    except ValueError as e:
        raise DecreeError(f"unparseable decree: {e}")
    if not isinstance(spec, dict):
        raise DecreeError("decree packet must be a JSON object")

    did = str(spec.get("id") or "").strip()
    if not DECREE_ID_RE.fullmatch(did):
        raise DecreeError("decree id must be 3-60 chars of letters/digits/._-")
    by = str(spec.get("by") or "").strip()
    if len(by) < 2:
        raise DecreeError("decree must name who ruled (`by`, e.g. \"M'Lord\")")
    at = ""
    if spec.get("at"):
        at = _parse_iso(str(spec.get("at"))) or ""
        if not at:
            raise DecreeError(f"unreadable `at` timestamp {spec.get('at')!r}")

    decisions = spec.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise DecreeError("decree must carry at least one decision")
    if len(decisions) > MAX_DECISIONS:
        raise DecreeError(f"a decree may carry at most {MAX_DECISIONS} decisions")

    normalized: list[dict] = []
    for index, item in enumerate(decisions, start=1):
        if not isinstance(item, dict):
            raise DecreeError(f"decision {index} is not a JSON object")
        quest = str(item.get("quest") or "").strip()
        if not quest:
            raise DecreeError(f"decision {index} is missing `quest`")
        ruling = str(item.get("ruling") or "").strip()
        if len(ruling) < MIN_RULING_CHARS:
            raise DecreeError(
                f"decision {index} ({quest}) needs a `ruling` of at least "
                f"{MIN_RULING_CHARS} characters")
        question = str(item.get("question") or "").strip()
        status = str(item.get("status") or "").strip().upper()
        if status and status not in SAFE_STATUSES:
            raise DecreeError(
                f"decision {index} ({quest}) sets status {status!r}; decrees may only "
                f"target {', '.join(SAFE_STATUSES)} (razing/shipping/pillory stay human acts)")
        normalized.append({
            "quest": quest,
            "question": question[:MAX_QUESTION_CHARS],
            "ruling": ruling,
            "status": status,
            # An operational directive is written to the ledger, not to a
            # charter: "muster a Steward for merge-blocker triage" is a work
            # order about the pipeline, not a ruling on one Quest.
            "directive": bool(item.get("directive")),
        })

    return {
        "id": did,
        "by": by,
        "at": at,
        "source": str(spec.get("source") or "").strip(),
        "decisions": normalized,
    }


def validate_decree(text: str, repo: Path) -> tuple[bool, str]:
    """A decree is acceptable when it parses and every quest it names exists.

    Unlike a charter, an existing quest's status does not freeze a decree: a
    ruling is exactly the thing that changes a commissioned Quest's course.
    """
    try:
        packet = parse_decree_packet(text)
    except DecreeError as e:
        return False, str(e)
    for item in packet["decisions"]:
        if quest_path(repo, item["quest"]) is not None:
            continue
        if re.match(r"^Q\d+", item["quest"], re.IGNORECASE):
            # A Quest-shaped id must resolve — "directive" is not a typo bypass.
            return False, f"decision names unknown quest {item['quest']!r} (no charter on castle)"
        if item.get("directive"):
            continue
        return False, (f"decision target {item['quest']!r} is not a Quest; add "
                       f"\"directive\": true to record it as an operational directive")
    return True, ""


_MULTI_QUEST_RE = re.compile(r"[,;]")


def expand_decisions(packet: dict) -> dict:
    """Split a decision that names several Quest ids into one decision per id.

    A voice session often answers one question over a SET of ids
    (``"Q408,Q508,..."``). The lane's contract is one decision per target, so
    the expansion is mechanical and preserves question/ruling/status verbatim.
    """
    out = {**packet, "decisions": []}
    for decision in packet.get("decisions") or []:
        parts = [p.strip() for p in
                 _MULTI_QUEST_RE.split(str(decision.get("quest") or "")) if p.strip()]
        if len(parts) <= 1:
            out["decisions"].append(decision)
        else:
            out["decisions"].extend({**decision, "quest": qid} for qid in parts)
    return out


def validate_decree_for_lane(text: str, repo: Path) -> tuple[bool, str]:
    """Pickup-lane decree validation: structure + expanded targets.

    Stricter than :func:`validate_decree` in exactly one way: a multi-id
    ``quest`` field is expanded and EVERY id must resolve. The lane never
    applies a ruling to a partial target set — a packet that names one dead id
    is recorded as skipped so the rest of the batch still lands, and the
    remote author can resend it corrected.
    """
    try:
        packet = expand_decisions(parse_decree_packet(text))
    except DecreeError as e:
        return False, str(e)
    unresolved: list[str] = []
    for item in packet["decisions"]:
        if quest_path(repo, item["quest"]) is not None:
            continue
        if re.match(r"^Q\d+", item["quest"], re.IGNORECASE):
            unresolved.append(item["quest"])
            continue
        if item.get("directive"):
            continue
        return False, (f"decision target {item['quest']!r} is not a Quest; add "
                       f"\"directive\": true to record it as an operational directive")
    if unresolved:
        return False, ("decision names unknown quest(s) "
                       + ", ".join(repr(q) for q in unresolved)
                       + " (no charter on castle); fix the id(s) and resend — a "
                         "ruling is never applied to a partial target set")
    return True, ""


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def default_status_for(current: str) -> str:
    """A ruling that leaves the target implicit still unblocks a HELD Quest."""
    return "WORKING" if (current or "").strip().upper() == "HELD" else ""


def _audience_block(packet: dict, decision: dict, applied_at: str) -> str:
    lines = [f"### {applied_at} — Royal Decree `{packet['id']}` (via Pleasance)"]
    if decision.get("question"):
        lines.append(f"- **Question:** {decision['question']}")
    lines.append(f"- **Ruling:** {decision['ruling']}")
    lines.append(f"- **By:** {packet['by']}" + (f" ({packet['source']})" if packet.get("source") else ""))
    target = decision.get("status") or ""
    if target:
        lines.append(f"- **Resulting status:** {target}")
    return "\n".join(lines)


def apply_to_quest(quest, packet: dict, decision: dict,
                   applied_at: Optional[str] = None) -> str:
    """Append the ruling to the Quest's Audience Log and apply its status.

    Returns the status actually applied ("" when the Quest's stage is unchanged).
    """
    at = packet.get("at") or applied_at or now_iso()
    target = decision.get("status") or default_status_for(quest.status)
    block = _audience_block(packet, {**decision, "status": target}, at)
    note = f"Royal decree `{packet['id']}` via Pleasance: {decision['ruling'][:160]}"
    quest.set_section("Audience Log", block, mode="append")
    if target:
        quest.set_status(target, note=note)
    else:
        quest.log_ledger(quest.status, quest.status, note)
    return target


def apply_packets(repo: Path, packets: list[dict],
                  court_root: Optional[Path] = None) -> dict:
    """Apply validated packets to the castle charters, without committing.

    Returns ``{"applied": [...], "paths": [Path...], "decree_ids": [...]}``.
    ``paths`` are the charter/event-journal files the caller must include in
    the pickup's single atomic commit.
    """
    root = court_root or Path(repo) / ".court"
    applied_at = now_iso()
    applied: list[dict] = []
    changed: list[Path] = []
    decree_ids: list[str] = []
    for packet in packets:
        for decision in packet["decisions"]:
            if (decision.get("directive")
                    and quest_path(repo, decision["quest"]) is None):
                # Operational directive: journal it, change no charter.
                directives_dir = root / "directives"
                directives_dir.mkdir(parents=True, exist_ok=True)
                safe = re.sub(r"[^A-Za-z0-9._-]+", "-", decision["quest"]).strip("-")
                path = directives_dir / f"{safe}.md"
                block = [f"# Directive — {decision['quest']}", "",
                         f"- **Decree:** `{packet['id']}` ({applied_at})",
                         f"- **By:** {packet['by']}"
                         + (f" ({packet['source']})" if packet.get("source") else "")]
                if decision.get("question"):
                    block.append(f"- **Question:** {decision['question']}")
                block += ["", decision["ruling"], ""]
                with path.open("a", encoding="utf-8") as fh:
                    fh.write("\n".join(block))
                changed.append(path)
                applied.append({
                    "decree": packet["id"], "quest": decision["quest"],
                    "from": "DIRECTIVE", "to": "DIRECTIVE",
                    "ruling": decision["ruling"][:200],
                })
                continue
            quest = store.load(decision["quest"], court_root=root)
            before = quest.status
            after = apply_to_quest(quest, packet, decision, applied_at=applied_at)
            changed.extend(store._write(quest, root))
            applied.append({
                "decree": packet["id"],
                "quest": quest.id,
                "from": before,
                "to": after or before,
                "ruling": decision["ruling"][:200],
            })
        decree_ids.append(packet["id"])
    return {"applied": applied, "paths": changed,
            "decree_ids": list(dict.fromkeys(decree_ids))}


# ---------------------------------------------------------------------------
# The decision brief (what needs a royal ruling right now)
# ---------------------------------------------------------------------------


def _clip(text: str, limit: int = 600) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _last_meaningful_block(section: str, limit: int = 600) -> str:
    """The last `###` block (or last non-empty paragraph) of a body section."""
    text = (section or "").strip()
    if not text:
        return ""
    blocks = re.split(r"\n(?=###\s)", text)
    for block in reversed(blocks):
        cleaned = _clip(block, limit)
        if cleaned:
            return cleaned
    return ""


def _question_for(quest) -> str:
    try:
        audience = quest.extract_audience()
    except Exception:  # noqa: BLE001 — telemetry must never crash collection
        audience = ""
    if audience and audience.strip().lower().rstrip(".:") not in (
            "none", "none required", "no audience required", "n/a"):
        return _clip(audience)
    log = _last_meaningful_block(quest.body_sections.get("Audience Log", ""))
    if log:
        return log
    if quest.status == "PUNISHED":
        judgement = _clip(quest.body_sections.get("Judgement of the Condemned", ""))
        if judgement:
            return judgement
    ledger = quest.body_sections.get("Castle Ledger", "")
    for line in reversed((ledger or "").splitlines()):
        if "HELD" in line and "—" in line:
            return _clip(line.split("—", 1)[-1])
    return ""


def _next_step(kind: str, quest_id: str, status: str) -> str:
    if kind == "ui":
        return f"`court ui-signoff {quest_id} --by \"M'Lord\"` (or `/artist` for polish)"
    if kind == "audience":
        return f"rule on the question; the Audience Log entry clears the pending flag ({status})"
    if status == "HELD":
        return f"rule; a decree without an explicit status resumes it at WORKING"
    return "rule; the successor Quest is chartered from the ruling"


def collect(repo: Path, *, snap: Optional[dict] = None,
            ui_pending_ids: Optional[set] = None,
            court_root: Optional[Path] = None) -> list[dict]:
    """The Audience frontier: every non-terminal Quest awaiting M'Lord.

    Deterministic and model-free. ``snap`` (``court status --json`` shape) is
    optional so the pulse can reuse the snapshot it already paid for; the
    quest charters are read only for the question text of flagged records.
    """
    repo = Path(repo)
    root = court_root or repo / ".court"

    if snap is not None:
        metas = [q for q in (snap.get("quests") or []) if q.get("kind") != "epic"]
    else:
        metas = []
        for quest in store.list_all(include_archive=False, court_root=root):
            if quest.kind == "epic":
                continue
            metas.append({
                "id": quest.id,
                "title": quest.title,
                "status": quest.status,
                "pending_audience": quest.has_pending_audience(),
            })

    if ui_pending_ids is None:
        ui_pending_ids = set()
        for meta in metas:
            status = (meta.get("status") or "").upper()
            if status in ("TRIBUTE_READY", "GATE"):
                try:
                    from . import convoy_guard
                    if convoy_guard.ui_review_pending(repo, meta["id"]):
                        ui_pending_ids.add(meta["id"])
                except Exception:  # noqa: BLE001 — UI extraction is advisory
                    pass
    ui_pending_ids = set(ui_pending_ids or [])

    items: list[dict] = []
    for meta in metas:
        qid = meta.get("id") or ""
        status = (meta.get("status") or "").upper()
        if not qid or status in _TERMINAL:
            continue
        pending_audience = bool(meta.get("pending_audience"))
        if status == "HELD":
            kind = "blocked"
        elif status == "PUNISHED":
            kind = "blocked"
        elif qid in ui_pending_ids:
            kind = "ui"
        elif pending_audience:
            kind = "audience"
        else:
            continue

        question = ""
        try:
            question = _question_for(store.load(qid, court_root=root))
        except Exception:  # noqa: BLE001 — a snap-only id may not be on disk yet
            pass
        if not question:
            question = {
                "blocked": "blocked on a royal decision (side-state)",
                "audience": "the Serf documented an open decision requiring royal judgment",
                "ui": "the UI review is still outstanding",
            }[kind]
        items.append({
            "id": qid,
            "title": meta.get("title") or "",
            "status": status,
            "kind": kind,
            "question": _clip(question),
            "next": _next_step(kind, qid, status),
        })

    items.sort(key=lambda item: (_KIND_ORDER.get(item["kind"], 9), item["id"]))
    return items


def decree_skeleton(item: dict, index: int) -> dict:
    decision = {
        "quest": item["id"],
        "question": _clip(item.get("question") or "", 160),
        "ruling": "<the ruling in M'Lord's words>",
    }
    if item.get("kind") == "blocked" and item.get("status") == "HELD":
        decision["status"] = "WORKING"
    return {
        "id": f"decree-{now_iso()[:10]}-{index}",
        "by": "M'Lord",
        "source": "voice via the Pleasance agent",
        "decisions": [decision],
    }


def render_markdown(items: list[dict], repo_name: str = "") -> str:
    """The full decision brief: how to review, then one answerable block each."""
    lines = [
        f"# Decisions awaiting M'Lord — {repo_name or 'the Court'}",
        "",
        "Each decision below is deterministic state read from the board, not a "
        "model's summary. Answer a whole round in one decree packet:",
        "",
        "1. Save a packet as `.court/decrees/<id>.json` and commit it to the "
        "`pleasance` branch (voice-chat answers are fine — one decision entry each).",
        "2. The Night's Watch runs `court pickup` each tick; valid packets are applied "
        "onto `castle` in one atomic commit: the ruling lands in the Quest's "
        "`## Audience Log`, and any `status` clears the stall.",
        "3. A packet may name an unknown Quest only if it has a charter on castle; "
        "a ruling may only target "
        + ", ".join(SAFE_STATUSES) + ".",
        "",
        "```json",
        json.dumps({
            "id": "decree-2026-11-05-1200",
            "by": "M'Lord",
            "source": "voice via the Pleasance agent",
            "decisions": [
                {"quest": "Q123-Slug", "question": "Which auth model?",
                 "ruling": "Session cookies; no JWT.", "status": "WORKING"},
            ],
        }, indent=2),
        "```",
        "",
    ]
    if not items:
        lines += ["No decisions are waiting — nothing needs a royal ruling right now.", ""]
        return "\n".join(lines)

    for index, item in enumerate(items, start=1):
        lines += [
            f"## D{index} · `{item['id']}` — {item['title'] or '(untitled)'}",
            "",
            f"- **Status:** {item['status']} · **Kind:** {item['kind']}",
            f"- **Question:** {_clip(item.get('question') or '', 600)}",
            f"- **When ruled:** {item.get('next') or ''}",
            "",
            "```json",
            json.dumps(decree_skeleton(item, index), indent=2),
            "```",
            "",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_decisions(args) -> None:
    """List pending decisions (the Audience frontier) for review."""
    repo = Path(getattr(args, "repo", None) or git_ops.get_repo_root()).resolve()
    items = collect(repo)
    limit = max(1, int(getattr(args, "limit", 20) or 20))
    if getattr(args, "json", False):
        print(json.dumps(items[:limit], indent=2))
        return
    print(render_markdown(items[:limit], repo.name))


def cmd_decree(args) -> None:
    """Author (or extend) a decree packet from explicit rulings."""
    repo = Path(getattr(args, "repo", None) or git_ops.get_repo_root()).resolve()
    stamp = now_iso()
    did = (getattr(args, "id", "") or "").strip() or f"decree-{stamp[:10]}-{stamp[11:19].replace(':', '')}"
    target = decree_dir(repo) / f"{did}.json"
    packet: dict = {}
    if target.exists():
        try:
            packet = parse_decree_packet(target.read_text(encoding="utf-8"))
        except (OSError, DecreeError) as e:
            print(f"ERROR: existing decree {target.name} is unreadable: {e}", file=sys.stderr)
            raise SystemExit(1)
    packet = {
        "id": did,
        "by": getattr(args, "by", "") or "M'Lord",
        "at": packet.get("at") or now_iso(),
        "source": packet.get("source") or "voice via the Pleasance agent",
        "decisions": list(packet.get("decisions") or []) + [{
            "quest": args.quest,
            "question": getattr(args, "question", "") or "",
            "ruling": args.ruling,
            "status": (getattr(args, "status", "") or "").strip().upper(),
        }],
    }
    ok, why = validate_decree(json.dumps(packet), repo)
    if not ok:
        print(f"ERROR: {why}", file=sys.stderr)
        raise SystemExit(1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")

    # Apply the ruling immediately on this checkout: a locally authored packet
    # lives on castle, so the pleasance pickup would never see it. Apply ONLY
    # the new decision so extending an already-applied packet is not replayed.
    applied = apply_packets(repo, [{**packet, "decisions": [packet["decisions"][-1]]}])
    print(f"wrote {target.relative_to(repo)}")
    for row in applied["applied"]:
        print(f"  {row['quest']}: {row['from']} → {row['to']} — {row['ruling']}")
    # Remember the id outside the tree so a later delivery of the same packet
    # over the pleasance lane is skipped rather than replayed.
    try:
        from . import pickup
        state = pickup.load_state(repo)
        ids = list(dict.fromkeys(list(state.get("applied_decrees") or []) + [did]))
        state["applied_decrees"] = ids[-500:]
        pickup.save_state(repo, state)
    except Exception:  # noqa: BLE001 — local authoring must not depend on watch state
        pass
    if not getattr(args, "no_commit", False):
        paths = [target] + list(applied["paths"])
        res = git_ops.git_commit_paths(paths, f"court: decree {did}", cwd=repo)
        if not res.get("ok") and not res.get("no_changes"):
            print(f"WARNING: autocommit failed: {res.get('stderr') or res.get('warning')}",
                  file=sys.stderr)
