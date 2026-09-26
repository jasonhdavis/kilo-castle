#!/usr/bin/env python3
"""
Reconcile pre-Phase-1 divergent Quest/Epic ledgers into clean initial event logs.

Phase 2 of the 2026-09-05 Court engine re-architecture (Q186) — companion to
eventlog.py / store.py (Phase 1).

--------------------------------------------------------------------------
The problem this solves
--------------------------------------------------------------------------
Phase 1 made `<id>.events.jsonl` the source of truth with `merge=union`.
Any quest touched by any CLI command from now on auto-backfills a clean
event log from whatever its OWN branch's current `<id>.md` contains at that
moment. That is NOT sufficient for quests already left genuinely divergent
before Phase 1 landed (the 2026-09-05 cogship-009 / gatehouse-east incident:
a false `(FORCED - UNMERGED)` promotion was corrected by hand in castle's
copies via ledger re-editing while the Serf worktree branches still carried
their own divergent copies). If each divergent branch later auto-backfills
its own stale `.md` into its own event log, `merge=union` faithfully keeps
BOTH versions as separate timestamped events — and a timestamp-sorted fold
can pick the WRONG side whenever the stale backfill's timestamp is newer.
Auto-backfill alone launders pre-existing divergence into the new format
instead of resolving it. This script resolves it properly, once, before
that happens.

--------------------------------------------------------------------------
What it compares
--------------------------------------------------------------------------
For every ACTIVE quest/epic (`.court/quests/` + `.court/epics/` on the
`castle` ref), it parses both sides with `models.Quest.from_markdown` and
diffs field-by-field / section-by-section (never a raw text diff, so
whitespace-only re-serialization does not count as divergence):

  1. castle's copy                 — via `git show castle:<path>` (the
                                     canonical ledger of record).
  2. the quest's OWN branch        — the branch registered in the quest's
                                     `branch:` frontmatter (or any open
                                     worktree branch matching the quest's
                                     short id). This is the only other
                                     branch where `store.save()` permits
                                     writes (the Q149 cross-branch guard),
                                     so it is the only branch whose copy
                                     can ever auto-backfill itself.
  3. the four gatehouse stations   — `the-gatehouse/{north,south,east,west}`
                                     open worktrees (integration layer;
                                     cogship-009 was packed on east).
  4. every OTHER open worktree     — reported as stale pre-rebase snapshot
                                     drift only. Those branches cannot save
                                     other quests' files (Q149 guard), their
                                     copies are superseded when they rebase,
                                     and `<id>.md` is a cosmetic derived
                                     view post-Phase-1 — so reconciling
                                     them would be meaningless churn.

--------------------------------------------------------------------------
Ground-truth policy (per field / per section — the judgment calls)
--------------------------------------------------------------------------
Signals used (never blind "looks newer"): `git merge-base --is-ancestor
<branch> castle` (+ `git cherry` patch-equivalence) — i.e. exactly what
`court verify-merged` reports — and the canonical pipeline order for status.

If the quest's own branch tip is already merged into castle, castle's copy
is authoritative going forward for EVERY target (its copy is the integrated
superset; the branch's divergences are pre-integration history).

Otherwise, per target:
  - field:status         whichever side is FURTHER along the canonical
                         pipeline wins (legacy names normalized: REVIEW ->
                         TRIBUTE_READY, READY_FOR_TEARDOWN -> READY_TO_RAZE);
                         ties -> castle. This picks up a Serf's legitimate
                         self-advance (WORKING -> TRIBUTE_READY) while never
                         letting a stale branch drag a quest backwards out
                         of a castle-side transition (GATE, stamping).
  - field:updated_at     max() of both sides.
  - field:created_at and identity fields (id, title, kind, app, concern,
    parent_epic, section, tags) and Court-bookkeeping fields the dispatching
    side has moved past (cogship_id, cogship_station,
    cogship_promoted_commit, gatekeeper_*, master_of_coin_*, serf_*,
    branch, pillory_of, pilloried_by, scout_of): castle wins.
  - field:worktree, field:task_file: prefer whichever value's path actually
    exists on disk (the registered worktree/task file that is real beats a
    stale or empty registration); castle on ties.
  - section:Castle Ledger  UNION of both sides' `- **ts** — ...` chronicle
    bullets, deduplicated by exact line, sorted by timestamp (castle first
    on ties). Both sides' bullets are real history: castle carries the
    post-incident correction rows, the worktree carries the Serf's own
    progress rows — neither is "wrong", so the ledger is unioned, not
    picked. Any non-bullet prose found in a Castle Ledger section is
    reported as a manual-review flag (the bullet union still proceeds).
  - every other body section (Tribute Rendered, Expected Tribute, Master of
    Coin's Audit, Cogship Log, Audience Log, The Kingdom Requires,
    Judgement of the Condemned):
      castle vs own branch only — if they differ and the branch carries real
      unmerged commits, the BRANCH wins (it is the Serf/Master-of-Coin
      worktree where that content was actually produced; castle's copy was
      shown by the 2026-09-05 incident to be able to LOSE content, e.g. a
      rendered Master of Coin audit). If a gatehouse station's copy of such
      a section differs from BOTH castle and the own branch, that is
      reported as a manual-review flag and NOT auto-adopted (a stale
      integration snapshot must never be able to inject prose).

--------------------------------------------------------------------------
What --apply writes
--------------------------------------------------------------------------
For each reconciled quest:
  - If NO event log exists yet: seed one clean initial `<id>.events.jsonl`
    built with `eventlog.build_events_for_save(reconciled_quest, prior=None,
    ts=seed_ts)` — one event per frontmatter field and per body section.
    `seed_ts` is the newest `updated_at` seen on any compared side plus one
    second: realistic-historical (the reconciliation logically happened
    immediately after the last real touch) and guaranteed to sort AFTER any
    equal-timestamp auto-backfill a stale branch might later produce.
  - If an event log already exists (auto-backfilled pre-reconciliation):
    append only corrective `field:`/`section:` events for targets where the
    reconciled value differs from the currently folded value, timestamped
    now (a genuinely newer correction on top of an existing log).
  - Regenerate `<id>.md` from the reconciled quest (the derived view must
    match the seeded log on whichever branch carries them).

Deliberate scope note on writing/commits: this script writes the reconciled
ledger files into the CURRENT checkout and commits them per-quest onto the
CURRENT branch (it refuses to run on `main` or `the-gatehouse/*`). That is
the designed Phase-2 flow: the reconciliation transaction is chartered as
its own Quest (Q186), lands on that Quest's branch, and reaches castle
through the normal `the-gatehouse/<station> -> castle` pipeline — it is NOT
a cross-branch save of other quests' work product (the Q149 guard concern),
it is the one-time migration artifact itself. `store.save()` is deliberately
NOT used per quest because its per-quest branch guard would (correctly)
refuse most of these writes from a foreign worktree.

Usage:
    python3 court/reconcile_diverged_quests.py --check
    python3 court/reconcile_diverged_quests.py --check --quest Q142
    python3 court/reconcile_diverged_quests.py --apply --quest Q142
    python3 court/reconcile_diverged_quests.py --apply
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # .court/

from court import eventlog, git_ops, store  # noqa: E402
from court.models import (  # noqa: E402
    DEFAULT_BODY_SECTIONS,
    FRONTMATTER_FIELDS,
    STATUSES,
    Quest,
)

CANONICAL_REF = "castle"
GATEHOUSE_STATIONS = ("north", "south", "east", "west")

# Frontmatter fields where castle (the dispatching/record authority) always
# wins a conflict with an unmerged worktree copy.
_CASTLE_ALWAYS_WINS_FIELDS = (
    "id", "title", "kind", "app", "concern", "parent_epic", "section", "tags",
    "created_at", "cogship_id", "cogship_station", "cogship_promoted_commit",
    "branch", "serf_session_id", "serf_model",
    "master_of_coin_session_id", "master_of_coin_model",
    "gatekeeper_session_id", "gatekeeper_model",
    "pillory_of", "pilloried_by", "scout_of",
)

# Fields resolved by "which value's path actually exists on disk".
_PATH_FIELDS = ("worktree", "task_file")

# Legacy status tokens (models.STATUS_LABELS) normalized to canonical ranks.
_STATUS_ALIASES = {"REVIEW": "TRIBUTE_READY", "READY_FOR_TEARDOWN": "READY_TO_RAZE"}
# HELD/PUNISHED are side-states, not pipeline ranks: if either side carries
# one, castle (which rendered the judgment) wins.
_SIDE_STATES = {"HELD", "PUNISHED"}

_LEDGER_BULLET_RE = re.compile(r"^- \*\*([^*]+)\*\* — ")
# Pre-rename ledger shape (models.py: "the one prior table — History — is now
# Castle Ledger, rendered as bullets"). Several open branch copies — notably
# the-gatehouse/east's cogship-009-era snapshots — still carry ledgers in
# (or partly in) this old table shape, and castle's bullet conversion during
# the 2026-09-05 hand-splicing is proven to have DROPPED some of those rows
# (e.g. Q142's original "Quest created"/"Chartered" rows survive only in
# east's table). Table rows are therefore first-class chronicle sources:
# converted to the canonical bullet form and unioned, not discarded.
_LEDGER_TABLE_ROW_RE = re.compile(
    r"^\|\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$")


def _ledger_chronicle_lines(section_text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Extract (ts, canonical_bullet) chronicle entries from a Castle Ledger
    section in either the current bullet shape or the legacy table shape.
    Returns (entries, unrecognized_prose_lines)."""
    entries: list[tuple[str, str]] = []
    prose: list[str] = []
    for line in section_text.splitlines():
        s = line.strip()
        if not s:
            continue
        # Legacy table scaffolding (header/separator rows) is structural junk
        # left behind by the partial table->bullet conversion — drop silently.
        if s in ("| ts | from | to | note |", "|---|---|---|---|"):
            continue
        m = _LEDGER_BULLET_RE.match(s)
        if m:
            entries.append((m.group(1), s))
            continue
        m = _LEDGER_TABLE_ROW_RE.match(s)
        if m:
            ts, frm, to, note = m.group(1), m.group(2), m.group(3), m.group(4)
            if frm in ("", "-", "—") or frm == to:
                bullet = f"- **{ts}** — {note}" if note else f"- **{ts}** — {to or frm or 'note'}"
            else:
                bullet = f"- **{ts}** — {frm} → {to}" + (f": {note}" if note else "")
            entries.append((ts, bullet))
            continue
        prose.append(s)
    return entries, prose


# --------------------------------------------------------------------------
# git helpers (mirror the `git show <ref>:<path>` cross-worktree reading
# pattern ward.py / cli.py diff use — content from refs, not from whichever
# checkout this process happens to sit in).
# --------------------------------------------------------------------------

def _run(args: list[str]) -> tuple[bool, str]:
    res = subprocess.run(args, capture_output=True, text=True)
    return res.returncode == 0, (res.stdout if res.returncode == 0 else res.stderr)


def _show(ref: str, path: str) -> Optional[str]:
    ok, out = _run(["git", "show", f"{ref}:{path}"])
    return out if ok else None


def open_worktree_branches() -> dict[str, str]:
    """branch name -> worktree path, for every open non-trunk worktree."""
    ok, out = _run(["git", "worktree", "list", "--porcelain"])
    if not ok:
        raise RuntimeError(f"git worktree list failed: {out}")
    out_map: dict[str, str] = {}
    wt_path = ""
    for line in out.splitlines():
        if line.startswith("worktree "):
            wt_path = line[len("worktree "):]
        elif line.startswith("branch "):
            branch = line[len("branch "):].replace("refs/heads/", "")
            if branch not in ("castle", "main"):
                out_map[branch] = wt_path
    return out_map


def _branch_matches_quest(branch: str, short: str) -> bool:
    """True if an open branch plausibly IS this quest's own session branch.
    Matches the quest's short id as the FINAL path component's id segment
    (quest/q138/q142-... -> 'q142-...'), or an Agent Manager sanitized flat
    name (quest-q173-...-2). A bare '/<epic-folder>/' prefix must NOT match
    (Q137 lives at quest/q125/q137-... — it is not Q125's own branch)."""
    last = branch.split("/")[-1]
    if last == short or last.startswith(f"{short}-"):
        return True
    return bool(re.match(rf"^(quest|scout|epic)-{re.escape(short)}-", branch))


def active_quest_paths() -> list[str]:
    ok, out = _run(["git", "ls-tree", "-r", CANONICAL_REF, "--name-only",
                    ".court/quests", ".court/epics"])
    if not ok:
        raise RuntimeError(f"git ls-tree {CANONICAL_REF} failed: {out}")
    return [l.strip() for l in out.splitlines() if l.strip().endswith(".md")]


def branch_merged_into_castle(branch: str) -> bool:
    ok, _ = _run(["git", "merge-base", "--is-ancestor", branch, CANONICAL_REF])
    if ok:
        return True
    # Patch-equivalence: not a literal ancestor, but every commit is already
    # present in castle in rebased form (git cherry emits '- ' for those).
    ok2, out = _run(["git", "cherry", CANONICAL_REF, branch])
    return ok2 and not [l for l in out.splitlines() if l.startswith("+")]


def unmerged_commit_counts(branch: str) -> tuple[int, int]:
    """(raw unmerged commit count, patch-unique count) vs castle."""
    _, out = _run(["git", "log", "--oneline", f"{CANONICAL_REF}..{branch}"])
    raw = len([l for l in out.splitlines() if l.strip()])
    _, out2 = _run(["git", "cherry", CANONICAL_REF, branch])
    unique = len([l for l in out2.splitlines() if l.startswith("+")])
    return raw, unique


def _parse_from_text(text: str, label: str) -> Quest:
    try:
        return Quest.from_markdown(text)
    except Exception as e:  # noqa: BLE001 — report and skip, like store.list_all
        raise RuntimeError(f"failed to parse {label}: {e}") from e


# --------------------------------------------------------------------------
# Target-level diffing and resolution
# --------------------------------------------------------------------------

def diff_targets(cq: Quest, oq: Quest) -> list[tuple[str, str]]:
    """Logical (non-whitespace) differences between two parsed quests:
    a list of ('field', name) / ('section', name) targets."""
    diffs: list[tuple[str, str]] = []
    for f in FRONTMATTER_FIELDS:
        if (str(getattr(cq, f, "") or "").strip()
                != str(getattr(oq, f, "") or "").strip()):
            diffs.append(("field", f))
    for s in DEFAULT_BODY_SECTIONS:
        if (cq.body_sections.get(s, "") or "").strip() \
                != (oq.body_sections.get(s, "") or "").strip():
            diffs.append(("section", s))
    return diffs


def _status_rank(status: str) -> Optional[int]:
    status = _STATUS_ALIASES.get(status, status)
    return STATUSES.index(status) if status in STATUSES else None


def _repo_roots() -> list[Path]:
    """Roots against which relative registered paths (worktree/task_file) are
    resolved: the current checkout AND the main checkout (recorded paths like
    '.kilo/worktrees/...' live under the main checkout, which from inside a
    nested Agent Manager worktree is three levels up: <main>/.git's parent)."""
    roots = [Path(git_ops.get_repo_root())]
    ok, out = _run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"])
    if ok and out.strip():
        common = Path(out.strip())          # <main>/.git
        roots.append(common.parent)         # <main>
    seen: set[str] = set()
    out_roots = []
    for r in roots:
        if str(r) not in seen:
            seen.add(str(r))
            out_roots.append(r)
    return out_roots


def _path_exists(value: str) -> bool:
    v = (value or "").strip()
    if not v:
        return False
    p = Path(v)
    if p.is_absolute():
        return p.exists()
    for root in _repo_roots():
        if (root / v).exists():
            return True
    return False


def _union_ledger(castle_text: str, other_texts: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """Union every side's Castle Ledger chronicle entries (current bullet
    shape AND legacy table shape, converted): dedupe by canonical bullet
    text, sort by timestamp (castle first on ties). Returns the rebuilt
    section plus manual-review flags for any non-chronicle prose."""
    flags: list[str] = []
    seen: set[str] = set()
    ordered: list[tuple[str, int, str]] = []  # (ts, side_order, bullet)
    for side_order, (side_label, text) in enumerate(
            [(CANONICAL_REF, castle_text)] + list(other_texts)):
        entries, prose = _ledger_chronicle_lines(text)
        for ts, bullet in entries:
            if bullet not in seen:
                seen.add(bullet)
                ordered.append((ts, side_order, bullet))
        for p in prose:
            flags.append(
                f"non-chronicle prose found in Castle Ledger on {side_label} "
                f"(kept out of the union — review manually): {p[:90]!r}")
    ordered.sort(key=lambda t: (t[0], t[1]))
    return "\n".join(b for _, _, b in ordered), flags


def resolve_quest(
    cq: Quest,
    own: Optional[tuple[str, Quest, str]],     # (branch, parsed, worktree path)
    gatehouse: list[tuple[str, Quest, str]],   # [(branch, parsed, path)]
) -> tuple[Quest, list[str], list[str]]:
    """Build the reconciled Quest from castle + own-branch + gatehouse sides.

    Returns (reconciled_quest, decision_lines, manual_review_flags).
    """
    decisions: list[str] = []
    flags: list[str] = []
    rq = Quest.from_markdown(cq.to_markdown())  # start from castle's copy
    reasons: dict[str, str] = {}

    own_branch, oq, own_wt = own if own else (None, None, None)
    own_merged = branch_merged_into_castle(own_branch) if own_branch else True
    own_raw, own_unique = unmerged_commit_counts(own_branch) if own_branch else (0, 0)

    if own_branch and own_merged:
        decisions.append(
            f"own branch {own_branch} is fully merged into castle "
            f"(merge-base --is-ancestor / patch-equivalent) -> castle authoritative "
            f"for every target; worktree copy is pre-integration history only")
        # rq stays castle's copy wholesale; still union the ledger in case a
        # gatehouse station carries packing rows castle's hand-spliced copy lost.
        if gatehouse:
            gh_texts = [(b, g.body_sections.get("Castle Ledger", "") or "") for b, g, _ in gatehouse]
            union, gh_flags = _union_ledger(
                cq.body_sections.get("Castle Ledger", "") or "", gh_texts)
            flags.extend(gh_flags)
            if union.strip() != (cq.body_sections.get("Castle Ledger", "") or "").strip():
                rq.body_sections["Castle Ledger"] = union
                decisions.append(
                    "section:Castle Ledger -> union with gatehouse bullet(s) "
                    "(castle copy may have lost rows in the 2026-09-05 hand-splice)")
        return rq, decisions, flags

    # ---- frontmatter -----------------------------------------------------
    for f in FRONTMATTER_FIELDS:
        cv = str(getattr(cq, f, "") or "").strip()
        ov = str(getattr(oq, f, "") or "").strip() if oq else cv
        if cv == ov:
            continue
        if f == "status":
            cr, orr = _status_rank(cv), _status_rank(ov)
            if cv in _SIDE_STATES or ov in _SIDE_STATES or cr is None or orr is None:
                chosen, who = cv, "castle"
            elif orr > cr:
                chosen, who = ov, "own branch"
            else:
                chosen, who = cv, "castle"
            setattr(rq, f, chosen)
            reasons[f"field:{f}"] = (
                f"{who} wins (pipeline position: castle={cv}, branch={ov}; "
                f"further-along wins, ties -> castle)")
        elif f == "updated_at":
            chosen = max(cv, ov)  # ISO-8601 Z strings sort lexically == chronologically
            setattr(rq, f, chosen)
            reasons[f"field:{f}"] = f"max(castle={cv}, branch={ov})"
        elif f in _CASTLE_ALWAYS_WINS_FIELDS:
            reasons[f"field:{f}"] = f"castle wins (Court-bookkeeping/identity field; castle={cv!r}, branch={ov!r})"
        elif f in _PATH_FIELDS:
            if cv and ov and _path_exists(cv) != _path_exists(ov):
                chosen = cv if _path_exists(cv) else ov
                who = "castle" if chosen == cv else "own branch"
                reasons[f"field:{f}"] = f"{who} wins (registered path exists on disk)"
                setattr(rq, f, chosen)
            elif not cv and ov:
                setattr(rq, f, ov)
                reasons[f"field:{f}"] = f"own branch wins (castle never set it; branch={ov!r})"
            else:
                reasons[f"field:{f}"] = f"castle wins on tie/uncertainty (castle={cv!r}, branch={ov!r})"
        # any other unlisted field: castle's value already in rq; note it.
        else:  # pragma: no cover — FRONTMATTER_FIELDS is fully enumerated above
            reasons[f"field:{f}"] = "castle wins (unlisted field)"

    # ---- body sections ---------------------------------------------------
    for s in DEFAULT_BODY_SECTIONS:
        cv = (cq.body_sections.get(s, "") or "").strip()
        ov = ((oq.body_sections.get(s, "") or "").strip()) if oq else cv
        if cv == ov:
            continue
        if s == "Castle Ledger":
            gh_texts = [(b, g.body_sections.get(s, "") or "") for b, g, _ in gatehouse]
            union, gh_flags = _union_ledger(cq.body_sections.get(s, "") or "", gh_texts)
            flags.extend(gh_flags)
            rq.body_sections[s] = union
            n_added = (len(_ledger_chronicle_lines(union)[0])
                       - len(_ledger_chronicle_lines(cv)[0]))
            reasons[f"section:{s}"] = (
                f"union of castle + own-branch + gatehouse chronicle entries "
                f"(+{max(n_added, 0)} chronicle row(s) castle lacked; both sides' "
                f"history is real)")
        else:
            if own_unique > 0 and ov:
                rq.body_sections[s] = oq.body_sections.get(s, "") or ""
                reasons[f"section:{s}"] = (
                    f"own branch wins ({own_unique} patch-unique unmerged commit(s); "
                    f"branch carries work product castle's copy lacks: "
                    f"castle={len(cv)}ch vs branch={len(ov)}ch)")
            else:
                reasons[f"section:{s}"] = (
                    f"castle wins (no patch-unique unmerged work, or branch side empty: "
                    f"castle={len(cv)}ch vs branch={len(ov)}ch)")
            # A gatehouse snapshot differing from BOTH authoritative sides on a
            # prose section must never be auto-adopted — surface it instead.
            for gb, gq, _ in gatehouse:
                gv = (gq.body_sections.get(s, "") or "").strip()
                if gv and gv != cv and (oq is None or gv != ov):
                    flags.append(
                        f"gatehouse snapshot {gb} has divergent prose in section "
                        f"'{s}' differing from both castle and the own branch — "
                        f"NOT auto-adopted, review manually")

    # Castle Ledger union vs oq difference bookkeeping: make sure a union that
    # ended up identical to castle's copy is still recorded as "no change".
    decisions.append(f"own branch: {own_branch or '(none)'} — "
                     f"unmerged commits raw={own_raw}, patch-unique={own_unique}"
                     + (" (fully merged: castle authoritative)" if own_merged else ""))
    decisions.extend(f"{k} -> {v}" for k, v in reasons.items())
    return rq, decisions, flags


# --------------------------------------------------------------------------
# Event seeding / corrective appends
# --------------------------------------------------------------------------

def _plus_one_second(ts: str) -> str:
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (dt + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return store.now_iso() if hasattr(store, "now_iso") else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan_event_write(rq: Quest, md_path: Path, sides_updated_ats: list[str]) -> tuple[list[dict], str, str]:
    """Return (events_to_append, mode, seed_ts) for one reconciled quest.

    mode is 'seed' (no log yet: full initial log, historical ts) or
    'append' (log exists: only corrective targets, ts=now) or 'none'.
    """
    ev_path = eventlog.events_path_for(md_path)
    existing = eventlog.read_events(ev_path)
    if not existing:
        ts = _plus_one_second(max([t for t in sides_updated_ats if t] or [rq.updated_at]))
        events = eventlog.build_events_for_save(rq, None, ts)
        return events, "seed", ts
    prior = eventlog.fold_events(existing, rq.id, rq.kind)
    corrective = eventlog.build_events_for_save(rq, prior, store.now_iso())
    return corrective, ("append" if corrective else "none"), store.now_iso()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                      help="dry-run: print the reconciliation report, write nothing")
    mode.add_argument("--apply", action="store_true",
                      help="write reconciled event logs + regenerated .md into the "
                           "current checkout and commit them per quest")
    ap.add_argument("--quest", action="append", default=[],
                    help="limit to specific quest id(s); repeatable")
    args = ap.parse_args(argv)

    repo_root = Path(git_ops.get_repo_root())
    ok, out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    current_branch = out.strip() if ok else ""

    if args.apply and (current_branch == "main"
                       or current_branch.startswith("the-gatehouse/")):
        print(f"REFUSING --apply on '{current_branch}': run it on castle or a "
              f"quest/ branch (the reconciliation rides the normal pipeline).",
              file=sys.stderr)
        return 2

    wts = open_worktree_branches()
    gatehouse_branches = [b for b in wts if b.startswith("the-gatehouse/")]
    all_paths = active_quest_paths()
    wanted = {q.strip().upper() for q in args.quest} if args.quest else None

    print(f"Reconciliation scan: {len(all_paths)} active quest/epic file(s) on "
          f"'{CANONICAL_REF}' vs {len(wts)} open non-trunk worktree branch(es) "
          f"({len(gatehouse_branches)} gatehouse). Mode: "
          f"{'APPLY' if args.apply else 'CHECK (dry-run)'}\n")

    flagged = 0
    written_paths: list[Path] = []
    for path in all_paths:
        cq = _parse_from_text(_show(CANONICAL_REF, path), f"{CANONICAL_REF}:{path}")
        if wanted and not any(cq.id.upper().startswith(w) or w.startswith(cq.id.upper())
                              for w in wanted):
            continue
        md_path = repo_root / path

        # Classify this quest's open-worktree sides.
        short = cq.id.split("-")[0].lower()
        own_branch: Optional[str] = None
        notes: list[str] = []
        if cq.branch and cq.branch in wts:
            own_branch = cq.branch
        elif cq.worktree and Path(cq.worktree).is_dir():
            res = subprocess.run(["git", "-C", str(cq.worktree),
                                  "rev-parse", "--abbrev-ref", "HEAD"],
                                 capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip() in wts:
                own_branch = res.stdout.strip()
        if own_branch is None and cq.branch:
            # Registered branch exists as a ref but is not checked out in any
            # open worktree: out of --apply scope (the dispatch scopes
            # reconciliation to OPEN worktree branches) — report-only.
            okb, _ = _run(["git", "rev-parse", "--verify", cq.branch])
            if okb:
                notes.append(
                    f"registered branch {cq.branch} exists but is not checked out "
                    f"in any open worktree — not reconciled (report-only)")
        if own_branch is None and not notes:
            # No usable registration at all: fall back to id-segment matching
            # across open branches (segment-exact — an epic folder prefix like
            # quest/q125/q137-... is NOT Q125's own branch).
            for b in sorted(wts):
                if _branch_matches_quest(b, short):
                    own_branch = b
                    break

        own = None
        if own_branch and _show(own_branch, path) is not None:
            own = (own_branch,
                   _parse_from_text(_show(own_branch, path), f"{own_branch}:{path}"),
                   wts[own_branch])
        gh = []
        for gb in gatehouse_branches:
            gtxt = _show(gb, path)
            if gtxt is not None:
                gh.append((gb, _parse_from_text(gtxt, f"{gb}:{path}"), wts[gb]))

        # Stale-snapshot drift on unrelated open branches: report-only.
        unrelated = [b for b in wts
                     if b not in gatehouse_branches and b != (own_branch or "")
                     and _show(b, path) is not None]
        unrelated_divs = 0
        for b in unrelated:
            try:
                oq = _parse_from_text(_show(b, path), f"{b}:{path}")
                unrelated_divs += 1 if diff_targets(cq, oq) else 0
            except RuntimeError:
                pass

        # Real divergence?
        sides: list[tuple[str, Quest]] = ([(own[0], own[1])] if own else []) + \
                                         [(b, q) for b, q, _ in gh]
        div_sides = [(b, q) for b, q in sides if diff_targets(cq, q)]
        if not div_sides:
            continue
        flagged += 1

        rq, decisions, flags = resolve_quest(cq, own, gh)
        events, write_mode, seed_ts = plan_event_write(
            rq, md_path,
            [cq.updated_at, own[1].updated_at if own else ""] +
            [q.updated_at for _, q, _ in gh])

        print(f"### {cq.id}  (castle: status={cq.status}, cogship={cq.cogship_id or '-'})")
        for line in notes:
            print(f"    note: {line}")
        for line in decisions:
            print(f"    {line}")
        for b, q in div_sides:
            n = len(diff_targets(cq, q))
            print(f"    divergent side: {b} ({n} target(s))")
        if unrelated_divs:
            print(f"    (stale pre-rebase snapshot drift on {unrelated_divs} unrelated "
                  f"open branch(es) — report-only, resolved when those branches rebase)")
        for f in flags:
            print(f"    MANUAL REVIEW: {f}")
        if write_mode == "seed":
            print(f"    plan: SEED {len(events)} initial event(s) at {seed_ts} "
                  f"-> {eventlog.events_path_for(md_path).name} (+ regenerate {path.split('/')[-1]})")
        elif write_mode == "append":
            targets = ", ".join(e["target"] for e in events)
            print(f"    plan: APPEND {len(events)} corrective event(s) at {seed_ts} "
                  f"for targets: {targets}")
        else:
            print("    plan: NONE (event log already folded-consistent with reconciliation)")
        print()

        if args.apply and write_mode != "none":
            eventlog.append_events(eventlog.events_path_for(md_path), events)
            md_path.write_text(rq.to_markdown(), encoding="utf-8")
            res = git_ops.git_commit_paths(
                [md_path, eventlog.events_path_for(md_path)],
                f"court: reconcile {cq.id} ledger into clean event log (Q186 Phase 2)",
                cwd=repo_root)
            status = "committed" if res.get("ok") else "COMMIT FAILED"
            print(f"    -> {status}: {cq.id} "
                  f"({len(events)} event(s), {write_mode})")
            if not res.get("ok"):
                print(f"       {res.get('warning')}", file=sys.stderr)
            written_paths.append(md_path)

    if args.apply:
        print(f"\nAPPLIED: {len(written_paths)} quest(s) reconciled and committed on "
              f"'{current_branch}'. They reach castle via the normal "
              f"the-gatehouse/<station> -> castle pipeline; DO NOT push directly.")
    print(f"\n{flagged} quest(s) flagged with real (non-whitespace) divergence vs "
          f"own-branch/gatehouse sides.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
