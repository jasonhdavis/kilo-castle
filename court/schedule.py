"""Per-quest eligibility diagnostic (`court schedule`).

Surfaces WHY an eligible-but-unadvanced Quest has not advanced, reusing the
Night's Watch planner's own reason strings (`court.nightwatch.plan`) instead
of reimplementing a parallel reason tracker. The planner is called with the
same inputs the live daemon uses (snapshot, persisted state, live workers,
policy), and its notes ARE the verdicts; only the planner's silent waits
(cooldown windows, `retry_at` backoff) are filled in deterministically.

Every verdict is classified as one of:
    ACTION_NEEDED  — a human/Steward decision is blocking this quest
    QUEUED_AUTO    — the Night's Watch will advance it automatically once a
                     timer/capacity/backoff clears ("waiting", not "stuck")
    HELD_BY_POLICY — the current watch configuration (--enable) will not act
    IN_FLIGHT      — a worker owns it, or the plan would act this tick
and each reason additionally reports its constraint class: HUMAN,
LOCAL_CONCURRENCY (RAM/worktree occupancy), LOCAL_BUDGET (hourly/count caps),
PROVIDER_TRANSIENT (rate-limit/retry backoff after a failed action), or
TEMPORARY_COOLDOWN. Provider/rate-limit constraints are thereby kept distinct
from local memory/CPU concurrency limits.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import git_ops
from . import nightwatch
from . import vigils

# Planner note levels -> outermost verdict when multiple notes exist for a quest.
_NOTE_LEVEL_RANK = {"human": 0, "skip": 1, "wait": 2, "vigil": 3, "report": 4}


def _within(ts: Optional[float], window: int, now: float) -> bool:
    return bool(ts) and (now - float(ts)) < window


def _retry_at(state: dict, qid: str) -> float:
    return float(((state.get("quests") or {}).get(qid) or {}).get("retry_at") or 0)


def _classify_text(text: str) -> str:
    low = text.lower()
    if "hourly spawn budget" in low:
        return "LOCAL_BUDGET"
    if "worker ceiling reached" in low or "busy/" in low:
        return "LOCAL_CONCURRENCY"
    if "budget exhausted" in low or "ceiling reached" in low or "needs a human" in low:
        return "HUMAN"
    if "merge" in low and ("conflict" in low or "held for" in low):
        return "HUMAN"
    if "disabled" in low:
        return "POLICY_DISABLED"
    if "cooldown" in low:
        return "TEMPORARY_COOLDOWN"
    if "rate" in low or "429" in low or "backoff" in low or "retry" in low:
        return "PROVIDER_TRANSIENT"
    if "planner" in low or "watch" in low:
        return "AUTO_ADVANCE"
    return "HUMAN"


def diagnose(repo: Path, status_filter: Optional[set[str]] = None,
             policy=None) -> tuple[list, dict]:
    """One-shot eligibility pass over the target repo. Returns (verdicts, meta).

    Pure read: runs the same inputs through the planner but never executes an
    action and never mutates watch state or the ledger."""
    snap = nightwatch.snapshot(repo)
    state = nightwatch.load_state(repo)
    workers = nightwatch.live_kilo_workers(repo)

    recorded = state.get("policy") if isinstance(state.get("policy"), dict) else {}
    if policy is None:
        kwargs = {k: v for k, v in recorded.items() if hasattr(nightwatch.Policy, k)}
        policy = nightwatch.Policy(**kwargs)
    now = time.time()

    try:
        qmap = nightwatch._quest_map(snap)
        dt = datetime.now(timezone.utc)
        ready, vigil_notes = vigils.ready_vigils(repo, qmap, now=dt)
        charter_ready = nightwatch._charter_ready(repo, qmap, now=dt)
    except Exception:  # noqa: BLE001 — observation must never kill the diagnostic
        ready, vigil_notes, charter_ready = [], [], []
    actions, notes = nightwatch.plan(
        snap, state, policy, workers, now=now,
        ready_vigils=ready, charter_ready=charter_ready)

    planned = {qid for a in actions for qid in a.quest_ids}
    by_quest: dict[str, list] = {}
    for n in notes:
        qid = (n.get("quest") or "").split(",")[0]
        if qid and qid != "board":
            by_quest.setdefault(qid, []).append(n)

    verdicts: list[dict] = []
    qstate = (state.get("quests") or {})
    for q in snap.get("quests", []):
        if q.get("kind") == "epic":
            continue
        if status_filter and q["status"] not in status_filter:
            continue
        qid = q["id"]
        st = qstate.get(qid) or {}
        quest_notes = sorted(by_quest.get(qid, []), key=lambda n: _NOTE_LEVEL_RANK.get(n.get("level"), 9))
        reason = quest_notes[0]["text"] if quest_notes else ""
        level = quest_notes[0].get("level") if quest_notes else ""
        constraint = _classify_text(reason) if reason else ""
        verdict = "IN_FLIGHT"
        if qid in planned:
            act = next(a for a in actions if qid in a.quest_ids)
            verdict = "IN_FLIGHT"
            reason = f"planner would act this tick: {act.kind} — {act.reason}"
            constraint = "AUTO_ADVANCE"
        elif level == "human":
            verdict = "ACTION_NEEDED"
        elif level in ("wait", "vigil"):
            verdict = "QUEUED_AUTO"
        elif level == "skip":
            if constraint == "POLICY_DISABLED":
                verdict = "HELD_BY_POLICY"
            elif constraint in ("HUMAN",):
                verdict = "ACTION_NEEDED"
            else:
                verdict = "HELD_CAPACITY"
        elif q.get("status") in ("WORKING", "QUESTING") and (q.get("worker") or {}).get("state") == "live":
            verdict = "IN_FLIGHT"
            reason = "worker is LIVE — task in progress"
            constraint = "IN_FLIGHT"
        else:
            # Planner was silent: mirror its deterministic gating conditions.
            retry = _retry_at(state, qid)
            if now < retry:
                verdict = "QUEUED_AUTO"
                reason = (f"FAILED-action retry backoff until "
                          f"{datetime.fromtimestamp(float(retry), tz=timezone.utc).strftime('%H:%M:%SZ')} "
                          f"(provider/rate-limit or transient failure)")
                constraint = "PROVIDER_TRANSIENT"
            elif _within(st.get("last_coin_ts"), int(getattr(policy, "coin_cooldown", 0)), now) and \
                    q.get("status") == "TRIBUTE_READY":
                verdict = "QUEUED_AUTO"
                reason = f"within the {int(getattr(policy, 'coin_cooldown', 0))}s coin cooldown (last attempt {time.strftime('%H:%M:%SZ', time.gmtime(float(st.get('last_coin_ts') or 0)))})"
                constraint = "TEMPORARY_COOLDOWN"
            elif _within(st.get("last_goad_ts"), int(getattr(policy, "goad_cooldown", 0)), now) and \
                    q.get("status") in ("WORKING", "QUESTING"):
                verdict = "QUEUED_AUTO"
                reason = f"within the {int(getattr(policy, 'goad_cooldown', 0))}s goad cooldown (last attempt {time.strftime('%H:%M:%SZ', time.gmtime(float(st.get('last_goad_ts') or 0)))})"
                constraint = "TEMPORARY_COOLDOWN"
            elif q.get("status") == "TRIBUTE_READY":
                verdict = "QUEUED_AUTO"
                reason = "planner silent this tick (not yet past its gates) — watch is considering it"
                constraint = "AUTO_ADVANCE"
            else:
                verdict = "QUEUED_AUTO"
                reason = "no planner note and no timer gate triggered — waiting on a turn or an upstream decision"
                constraint = "AUTO_ADVANCE"
        verdicts.append({
            "id": qid, "status": q["status"], "verdict": verdict,
            "constraint": constraint, "reason": reason[:240],
        })

    verdicts.sort(key=lambda v: (v["status"], v["id"]))
    meta = {
        "repo": str(repo),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "policy_line": policy.describe(),
        "live_workers": workers.get("count", 0),
        "max_workers": getattr(policy, "max_workers", 0),
        "planner_notes": len(notes),
        "planner_actions": [f"{a.kind}:{','.join(a.quest_ids)}" for a in actions],
    }
    return verdicts, meta


def render_table(verdicts: list[dict], meta: dict, quest_id: Optional[str] = None,
                 limit: int = 60) -> str:
    header = (
        "ROWS"
        if not quest_id
        else f"VERDICT — {quest_id}"
    )
    out = [
        f"⏳ Why is this quest waiting? — {meta['repo']}",
        f"   {meta['generated_at']} · {meta['policy_line']}",
        f"   live workers {meta['live_workers']}/{meta['max_workers']} · "
        f"planner notes {meta['planner_notes']}" + (f" · planner would act: {', '.join(meta['planner_actions'])}"
                                                    if meta['planner_actions'] else ""),
        "",
    ]
    for v in verdicts:
        if quest_id and v["id"] != quest_id:
            continue
        out.append(f"  [{v['status']:<13}] {v['id']}")
        out.append(f"      {v['verdict']:<18} {v['constraint']:<21} {v['reason']}")
    return "\n".join(out) + "\n"


def cmd_schedule(args) -> None:
    repo = Path(getattr(args, "repo", None) or git_ops.get_repo_root()).resolve()
    qid = getattr(args, "quest_id", None)
    status_filter = None
    if getattr(args, "status_filter", None):
        status_filter = {s.strip().upper() for s in args.status_filter.split(",") if s.strip()}
    elif not qid:
        status_filter = {"TRIBUTE_READY", "GATE", "WORKING", "QUESTING",
                         "CHARTERED", "DISPATCHED", "PLANNED"}
    verdicts, meta = diagnose(repo, status_filter=status_filter)
    if getattr(args, "json", False):
        print(json.dumps({"meta": meta, "verdicts": verdicts}, indent=2))
        return
    if qid:
        matches = [v for v in verdicts if v["id"] == qid]
        if not matches:
            print(f"schedule: no verdict for {qid} (is it outside the in-flight statuses?)",
                  file=sys.stderr)
            sys.exit(1)
    print(render_table(verdicts, meta, quest_id=qid, limit=int(getattr(args, "limit", 60) or 60)))