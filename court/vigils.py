"""Vigils: a Steward's bounded hand-off to the Night's Watch.

A vigil is a *gated* watch — "when <condition>, do <action>" — registered
deliberately by a Steward so the deterministic supervisor can carry it out
overnight. It exists because the honest answer to "watch for X and then do Y"
is not an LLM polling loop (burning a reasoning pass per check, dying with its
session) but a small durable record a token-free process re-reads.

Deliberately narrow, per M'Lord (2026-10-31): there are no indefinite watches.
Every vigil MUST carry

  * a human-readable ``reason`` — why the recurring look exists at all;
  * an ``expires_at`` (capped at MAX_VIGIL_HOURS) — a watch that nobody
    renews dies on its own;
  * a bounded ``max_fires`` (default 1) — a vigil announces a follow-on
    action, it does not become a daemon;
  * an allow-listed ``then`` action — no ship, no raze, no pillory, ever.

Conditions are deterministic predicates over durable state (quest status from
`court status --json`, branch ancestry in git, a file's existence, wall clock).
Nothing here needs a model, which is exactly why the Night's Watch can run it
at 3am for free.

Storage: one JSON file per vigil under ``.court/vigils/<id>.json`` — durable,
diffable, and committed with the rest of the Court's paperwork.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

VIGIL_DIR = "vigils"
MAX_VIGIL_HOURS = 24 * 7
DEFAULT_MAX_FIRES = 1
MAX_FIRES_CEILING = 5
MIN_REASON_CHARS = 12
VALID_STATUSES = (
    "OPEN", "PLANNED", "CHARTERED", "DISPATCHED", "QUESTING", "WORKING",
    "TRIBUTE_READY", "GATE", "READY_TO_RAZE", "LANDED", "LAUNCHED", "DONE",
    "HELD", "PUNISHED", "DEMOTED",
)
# Actions a vigil may take. Destructive or publication verbs (ship, raze,
# pillory, patchwork) are deliberately absent: a timer must never be the thing
# that publishes to production or freezes a quest.
ALLOWED_ACTIONS = ("dispatch", "goad", "coin", "collect", "advance", "note")
# Actions that spawn a model worker (and therefore consume RAM + the watch's
# spawn budgets). `note` and `advance` are bookkeeping only.
SPAWNING_ACTIONS = ("dispatch", "goad", "coin", "collect")


class VigilError(ValueError):
    """A registration that violates the narrowness contract."""


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_duration(text: str) -> Optional[timedelta]:
    """'90m' / '6h' / '2d' / '45s' -> timedelta."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", text or "")
    if not m:
        return None
    n = int(m.group(1))
    return {"s": timedelta(seconds=n), "m": timedelta(minutes=n),
            "h": timedelta(hours=n), "d": timedelta(days=n)}[m.group(2)]


def parse_expiry(spec: str, reference: Optional[datetime] = None) -> datetime:
    """Accept an absolute ISO timestamp or a relative span ('6h', '2d')."""
    ref = reference or now_utc()
    span = parse_duration(spec)
    if span is not None:
        return ref + span
    try:
        dt = datetime.fromisoformat((spec or "").replace("Z", "+00:00"))
    except ValueError:
        raise VigilError(f"cannot read expiry {spec!r}: use an ISO timestamp or a span like 6h/2d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_iso(text: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat((text or "").replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


def check_condition(cond: str, repo: Path, quests: dict, now: Optional[datetime] = None) -> tuple[bool, str]:
    """Evaluate one deterministic predicate -> (satisfied, explanation).

    `quests` is the id -> record map from `court status --json`; conditions
    never make a model call and never read the filesystem beyond `exists`."""
    now = now or now_utc()
    cond = (cond or "").strip()
    if not cond:
        return False, "empty condition"

    m = re.fullmatch(r"quest-status:([^=!]+)\s*(=|!=)\s*(\w+)", cond)
    if m:
        qid, op, want = m.group(1).strip(), m.group(2), m.group(3).upper()
        rec = quests.get(qid)
        if rec is None:
            # Fall back to prefix match: vigils are written by hand and a
            # short id (Q234) should find Q234-Long-Slug.
            matches = [q for q in quests.values() if q["id"] == qid or q["id"].startswith(qid + "-")]
            rec = matches[0] if len(matches) == 1 else None
        if rec is None:
            return False, f"{qid} not found"
        got = (rec.get("status") or "").upper()
        ok = (got == want) if op == "=" else (got != want)
        return ok, f"{qid} is {got}" + ("" if ok else f" (want {op} {want})")

    m = re.fullmatch(r"branch-merged:(.+)", cond)
    if m:
        branch = m.group(1).strip()
        return _is_ancestor(repo, branch, "castle")

    m = re.fullmatch(r"quest-merged:(.+)", cond)
    if m:
        qid = m.group(1).strip()
        rec = next((q for q in quests.values() if q["id"] == qid or q["id"].startswith(qid + "-")), None)
        if rec is None:
            return False, f"{qid} not found"
        branch = rec.get("branch") or ""
        if not branch:
            return False, f"{qid} has no branch"
        return _is_ancestor(repo, branch, "castle")

    m = re.fullmatch(r"file-exists:(.+)", cond)
    if m:
        p = (repo / m.group(1).strip()).resolve()
        try:
            p.relative_to(repo.resolve())
        except ValueError:
            return False, "path escapes the repository"
        return (p.exists(), f"{m.group(1).strip()} {'exists' if p.exists() else 'is missing'}")

    m = re.fullmatch(r"after:(.+)", cond)
    if m:
        when = _parse_iso(m.group(1).strip())
        if when is None:
            return False, f"unreadable timestamp {m.group(1)!r}"
        return (now >= when, f"now {'>=' if now >= when else '<'} {iso(when)}")

    return False, f"unknown condition {cond!r}"


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> tuple[bool, str]:
    try:
        res = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=str(repo), capture_output=True, text=True, timeout=20,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"git failed: {e}"
    if res.returncode == 0:
        return True, f"{ancestor} is merged into {descendant}"
    if res.returncode == 1:
        return False, f"{ancestor} is not yet in {descendant}"
    return False, f"git error: {(res.stderr or '').strip()[:120]}"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def validate(spec: dict) -> None:
    """Enforce the narrowness contract. Raises VigilError."""
    vid = (spec.get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,60}", vid):
        raise VigilError("id must be 3-60 chars of letters/digits/._-")
    reason = (spec.get("reason") or "").strip()
    if len(reason) < MIN_REASON_CHARS:
        raise VigilError(f"reason must be at least {MIN_REASON_CHARS} characters — say why the watch exists")
    when = spec.get("when") or []
    if not when:
        raise VigilError("at least one --when condition is required")
    for cond in when:
        if check_condition(cond, Path("."), {}, now_utc())[1].startswith("unknown condition"):
            raise VigilError(f"unrecognised condition: {cond}")
    then = (spec.get("then") or "").strip()
    verb = then.split(":", 1)[0].split("=", 1)[0].strip()
    if verb not in ALLOWED_ACTIONS:
        raise VigilError(f"action {verb!r} not allowed; choose from {', '.join(ALLOWED_ACTIONS)}")
    _parse_action(then)  # shape check

    mf = spec.get("max_fires")
    max_fires = DEFAULT_MAX_FIRES if mf is None else int(mf)
    if not 1 <= max_fires <= MAX_FIRES_CEILING:
        raise VigilError(f"max_fires must be 1..{MAX_FIRES_CEILING} — no indefinite watches")

    expires = _parse_iso(spec.get("expires_at") or "")
    if expires is None:
        raise VigilError("expires_at is required (absolute ISO or a span like 6h)")
    horizon = now_utc() + timedelta(hours=MAX_VIGIL_HOURS)
    if expires > horizon:
        raise VigilError(f"expires_at is beyond the {MAX_VIGIL_HOURS // 24}-day ceiling")
    if expires <= now_utc():
        raise VigilError("expires_at is already in the past")


def build(id: str, when: list, then: str, reason: str, expires: str,
          max_fires: int = DEFAULT_MAX_FIRES, created_by: str = "steward") -> dict:
    spec = {
        "version": 1,
        "id": id.strip(),
        "reason": reason.strip(),
        "when": [c.strip() for c in when if c and c.strip()],
        "then": then.strip(),
        "expires_at": iso(parse_expiry(expires)),
        "max_fires": int(max_fires),
        "created_by": created_by,
        "created_iso": iso(now_utc()),
        "fires": [],
        "status": "active",
    }
    validate(spec)
    return spec


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def vigils_dir(repo: Path) -> Path:
    return Path(repo) / ".court" / VIGIL_DIR


def path_for(repo: Path, vid: str) -> Path:
    return vigils_dir(repo) / f"{vid}.json"


def save(repo: Path, spec: dict) -> Path:
    p = path_for(repo, spec["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(spec, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return p


def load_all(repo: Path, include_finished: bool = False) -> list:
    out = []
    d = vigils_dir(repo)
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            spec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(spec, dict) or not spec.get("id"):
            continue
        if not include_finished and spec.get("status") != "active":
            continue
        out.append(spec)
    return out


def remove(repo: Path, vid: str) -> bool:
    try:
        path_for(repo, vid).unlink()
        return True
    except OSError:
        return False


def retire(repo: Path, spec: dict, status: str, note: str = "") -> None:
    spec["status"] = status
    spec["retired_iso"] = iso(now_utc())
    if note:
        spec["retired_note"] = note
    save(repo, spec)


def mark_fired(repo: Path, spec: dict, result: dict) -> None:
    spec.setdefault("fires", []).append({
        "ts": iso(now_utc()),
        "ok": bool(result.get("ok")),
        "exit": result.get("exit"),
        "seconds": result.get("seconds"),
    })
    if len(spec["fires"]) >= int(spec.get("max_fires") or 1):
        retire(repo, spec, "fired" if result.get("ok") else "active")
    else:
        save(repo, spec)


# ---------------------------------------------------------------------------
# Action grammar
# ---------------------------------------------------------------------------


def _parse_action(then: str) -> dict:
    """`then` grammar -> {verb, target, status}. Raises VigilError on shape."""
    then = (then or "").strip()
    if not then:
        raise VigilError("action (--then) is required")
    if then.startswith("note:"):
        text = then[5:].strip()
        if not text:
            raise VigilError("note: needs text")
        return {"verb": "note", "target": "", "status": ""}
    if then == "collect":
        return {"verb": "collect", "target": "", "status": ""}
    m = re.fullmatch(r"(\w+):([^=]+)=(\w+)", then)
    if m and m.group(1) == "advance":
        status = m.group(3).upper()
        if status not in VALID_STATUSES:
            raise VigilError(f"unknown status {status}")
        return {"verb": "advance", "target": m.group(2).strip(), "status": status}
    m = re.fullmatch(r"(\w+):(.+)", then)
    if m:
        verb, target = m.group(1), m.group(2).strip()
        if verb not in ALLOWED_ACTIONS:
            raise VigilError(f"action {verb!r} not allowed")
        if verb == "note":
            raise VigilError("use `note:<text>`")
        return {"verb": verb, "target": target, "status": ""}
    raise VigilError(f"unreadable action {then!r} (try `dispatch:Q234`)")


def action_argv(then: str) -> tuple[list, bool]:
    """Map a vigil action to (court argv, spawns_a_worker)."""
    a = _parse_action(then)
    verb, target, status = a["verb"], a["target"], a["status"]
    if verb == "note":
        return [], False
    if verb == "advance":
        return ["advance", target, status], False
    if verb == "collect":
        argv = ["collect"]
        if target:
            argv.append(target)
        argv.append("--standup")
        return argv, True
    if verb == "dispatch":
        # `court dispatch` creates the worktree from the base trunk (castle)
        # by default — that is the "dispatch from castle tip" semantics.
        return ["dispatch", target, "--standup"], True
    if verb == "goad":
        return ["goad", target], True
    if verb == "coin":
        return ["coin", target], True
    raise VigilError(f"unhandled action {verb!r}")


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def ready_vigils(repo: Path, quests: dict, now: Optional[datetime] = None) -> tuple[list, list]:
    """(ready, notes). `ready` = [(spec, why)] with every condition satisfied;
    expired vigils are retired in place and reported as notes."""
    now = now or now_utc()
    ready: list = []
    notes: list = []
    for spec in load_all(repo):
        expires = _parse_iso(spec.get("expires_at") or "")
        if expires and now >= expires:
            retire(repo, spec, "expired")
            notes.append({"id": spec["id"], "level": "expired",
                          "text": f"expired {iso(expires)} without firing ({spec.get('reason','')[:80]})"})
            continue
        reasons = []
        satisfied = True
        for cond in spec.get("when") or []:
            ok, why = check_condition(cond, repo, quests, now=now)
            reasons.append(f"{cond}: {why}")
            if not ok:
                satisfied = False
                break
        if satisfied:
            ready.append((spec, "; ".join(reasons)))
    return ready, notes
