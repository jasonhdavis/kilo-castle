"""Pleasance: remote-steward paperwork pickup for the Court.

The Pleasance lane is where an away Steward's voice-chat LLM delivers
paperwork: a SINGLE root branch named ``pleasance`` on origin carries ONLY
quest charters (``.court/quests/<id>.md``, plus their standard sibling
``.court/quests/<id>.events.jsonl`` journals) and vigil specs
(``.court/vigils/<id>.json``), committed directly by the LLM without ever
touching code. The Night's Watch "picks up" that paperwork on the local
`castle` trunk: fetch it, validate it, merge only the valid ``.court`` files
in ONE atomic commit, journal the outcome to ``.court/LEDGER.md``, and push
`castle` back to origin so the mirror stays current.

The lane is deliberately surgical:

  * the changed-path range is the remote author's OWN commits — the diff is
    taken against the fork point (``merge-base``), never the dramatically
    newer local `castle` tip, so unrelated drift never pollutes the batch;
  * every changed path in that range must be a quest charter, a quest event
    journal, a vigil spec, or one of the two root-level branch-surface docs
    (``AGENTS.md`` / ``PLEASANCE.md``) — anything else anywhere rejects the
    WHOLE batch (journaled once). Surface docs ride the branch so the
    GitHub-facing checkout is oriented, but they are NEVER merged onto castle
    (which keeps its own internal `AGENTS.md`); a batch that touches only them
    advances the applied marker without creating a trunk commit;
  * a dirty `castle` working tree rejects the whole batch — pickup refuses to
    mix its commit with unrelated local edits (only a conflicting dirty path
    refuses; uncommitted `.court` paperwork the pickup is not touching is
    normal pipeline churn and does not block the lane);
  * a changed charter must parse, carry a valid branch name, a status still
    pre-commission (``OPEN``/``PLANNED``/``CHARTERED``), and valid
    ``## Prerequisites`` predicates;
  * a charter for a quest that is ALREADY commissioned on `castle` (base status
    past ``CHARTERED``) is frozen — the base status is authoritative;
  * a vigil must validate under ``vigils.validate`` and must not collide with
    an identically-id ``status: "active"`` vigil already on `castle`.

Applied-state (which pleasance tip is already merged, the last rejection)
lives OUTSIDE the git tree, mirroring the Night's Watch's own state files —
never in the repo the pickup exists to keep clean.

Dispatch readiness is NOT decided here: a charter carrying
``status: CHARTERED`` whose ``## Prerequisites`` predicates are all satisfied
is dispatched by ``court.nightwatch`` — the charter itself is the authority,
no synthesized vigil file exists for this lane (see nightwatch.plan/tick).
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import decisions
from . import git_ops
from . import nightwatch
from . import store
from . import vigils
from .models import Quest, validate_branch_name

PICKUP_BRANCH = "pleasance"
STATE_VERSION = 1

# The only paths a pleasance batch may touch. Anything else under .court/
# (LEDGER.md, config, ...) or outside it rejects the whole pickup. A quest's
# sibling `<id>.events.jsonl` (append-only event journal, the project's
# standard charter companion) is in-lane alongside the charter. The two
# root-level branch-surface docs (AGENTS.md, PLEASANCE.md) may ALSO ride the
# branch so an external agent reading the pleasance checkout in GitHub gets the
# correct orientation; they are validated for presence only and are NEVER
# merged onto castle (which keeps its own internal AGENTS.md).
_QUEST_CHARTER_RE = re.compile(r"^\.court/quests/[^/]+\.(?:md|events\.jsonl)$")
_VIGIL_FILE_RE = re.compile(r"^\.court/vigils/[^/]+\.json$")
# A decree packet is M'Lord's answer to pending Audiences (see court.decisions).
_DECREE_FILE_RE = re.compile(r"^\.court/decrees/[^/]+\.json$")
_SURFACE_DOC_RE = re.compile(r"^(?:AGENTS\.md|PLEASANCE\.md)$")
# Statuses still open to Steward-driven dispatch. Anything past CHARTERED
# (DISPATCHED/WORKING/GATE/...) means the quest is commissioned and its
# charter is FROZEN — a remote LLM must never amend commissioned paperwork.
_OPEN_STATUSES = ("OPEN", "PLANNED", "CHARTERED")
_PREREQ_HEADER_RE = re.compile(r"^#{2,3}\s+Prerequisites:?\s*$", re.MULTILINE)
_PREREQ_LEAD_RE = re.compile(r"^-\s*(?:\[[ xX]\])?\s*")


def _run(repo: Path, argv: list, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *argv],
                          capture_output=True, text=True, timeout=timeout)


def _show(repo: Path, ref: str) -> str:
    """Resolve a ref to a commit sha, or '' when it does not exist."""
    res = _run(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
    return res.stdout.strip() if res.returncode == 0 else ""


def _content(repo: Path, ref: str, path: str) -> str:
    """`git show <ref>:<path>` raw content, or None when absent."""
    res = _run(repo, ["show", f"{ref}:{path}"])
    return res.stdout if res.returncode == 0 else None


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Applied state (OUTSIDE the git tree — mirrors nightwatch's state pattern)
# ---------------------------------------------------------------------------


def state_path(repo: Path) -> Path:
    """Durable applied-state file for this repo's pleasance pickup."""
    return nightwatch.state_dir(repo) / f"{nightwatch.repo_slug(repo)}-pickup.json"


def load_state(repo: Path) -> dict:
    """state = {version, applied_tip, last_attempt: {tip, reason}}."""
    p = state_path(repo)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("version") == STATE_VERSION:
            payload.setdefault("applied_tip", "")
            payload.setdefault("last_attempt", {"tip": "", "reason": ""})
            payload.setdefault("applied_decrees", [])
            return payload
    except (OSError, ValueError):
        pass
    return {"version": STATE_VERSION, "repo": str(repo),
            "applied_tip": "", "last_attempt": {"tip": "", "reason": ""},
            "applied_decrees": []}


def save_state(repo: Path, state: dict) -> None:
    p = state_path(repo)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Fetch / parse / validate
# ---------------------------------------------------------------------------


def fetch_pleasance(repo: Path, origin: str = "origin") -> tuple[bool, str]:
    """Fetch the remote pleasance branch. A repo whose origin has no such
    branch is a normal no-op: (False, reason) WITHOUT raising."""
    try:
        res = _run(repo, ["fetch", origin, PICKUP_BRANCH])
    except subprocess.TimeoutExpired:
        return False, f"timed out fetching {origin}/{PICKUP_BRANCH}"
    except Exception as e:  # noqa: BLE001
        return False, f"fetch failed: {e}"
    if res.returncode != 0:
        return False, (res.stderr or res.stdout or "").strip()[:200]
    return True, (res.stdout or "").strip()


def parse_prerequisites(quest_text: str) -> list[str]:
    """Parse the ``## Prerequisites`` body block of a charter into one
    predicate per line.

    Permissive by design: ``## Prerequisites``, ``## Prerequisites:`` and
    ``### Prerequisites`` all open the block, which runs until the next
    ``#``-level header or EOF. Each line is stripped of a leading ``- `` bullet
    and an optional markdown checkbox (``- [ ] `` / ``- [x] ``); empty lines
    are dropped. The remaining text is the predicate — validation happens at
    pickup time, not here."""
    m = _PREREQ_HEADER_RE.search(quest_text or "")
    if not m:
        return []
    out: list[str] = []
    for line in (quest_text or "")[m.end():].splitlines():
        if line.lstrip().startswith("#"):
            break
        stripped = line.strip()
        if not stripped:
            continue
        stripped = _PREREQ_LEAD_RE.sub("", stripped).strip()
        if not stripped:
            continue
        out.append(stripped)
    return out


def validate_prerequisite(cond: str, repo: Path) -> tuple[bool, str]:
    """A condition is valid syntax when ``check_condition`` does not reject it
    as an unknown condition. It is evaluated against an empty quest map, so
    'not found' style results are valid syntax (a precondition that names a
    quest yet to land is exactly the kind of ordering this lane expresses)."""
    _ok, why = vigils.check_condition(cond, repo, {}, vigils.now_utc())
    if why.startswith("unknown condition"):
        return False, why
    return True, why


def validate_charter(blob_text: str, repo: Path) -> tuple[bool, str]:
    """A charter is acceptable for pickup when it parses, its ``branch``
    frontmatter is a valid branch name, its ``status`` is still pre-commission
    (OPEN/PLANNED/CHARTERED), and every ``## Prerequisites`` line is a valid
    conditions predicate."""
    try:
        quest = Quest.from_markdown(blob_text)
    except ValueError as e:
        return False, f"unparseable charter: {e}"
    ok, why = validate_branch_name((quest.branch or "").strip())
    if not ok:
        return False, f"invalid branch {quest.branch!r}: {why}"
    status = (quest.status or "").strip().upper()
    if status not in _OPEN_STATUSES:
        return False, (f"status {quest.status!r} is not one of {', '.join(_OPEN_STATUSES)} "
                       f"— a quest that far along is commissioned and its charter is frozen")
    for cond in parse_prerequisites(blob_text):
        ok, why = validate_prerequisite(cond, repo)
        if not ok:
            return False, f"invalid prerequisite {cond!r}: {why}"
    return True, ""


def validate_vigil(blob_text: str, repo: Path) -> tuple[bool, str]:
    """A vigil spec must be parseable JSON and pass the vigils narrowness
    contract. Never raises on bad input."""
    try:
        spec = json.loads(blob_text)
    except ValueError as e:
        return False, f"unparseable vigil: {e}"
    try:
        vigils.validate(spec)
        return True, ""
    except Exception as e:  # noqa: BLE001 — VigilError/ValueError names the reason
        return False, str(e)


def validate_events_log(blob_text: str) -> tuple[bool, str]:
    """A quest's append-only event journal must be line-broken JSON records
    (the engine's fold_events shape). Empty/blank lines are tolerated; anything
    that is not a JSON object per line is foreign and rejected."""
    for lineno, raw in enumerate(blob_text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            return False, f"events journal line {lineno} is not JSON"
        if not isinstance(record, dict):
            return False, f"events journal line {lineno} is not a JSON object"
    return True, ""


# ---------------------------------------------------------------------------
# The pickup
# ---------------------------------------------------------------------------


def _resolve_tip(repo: Path, branch: str) -> str:
    """The current pleasance tip. The freshly-fetched remote-tracking ref is
    authoritative; the local branch (if any) is fast-forwarded to it so
    `git rev-parse <branch>` stays correct for the next run."""
    tip = _show(repo, f"origin/{branch}")
    if not tip:
        tip = _show(repo, branch)
    if tip and _show(repo, branch) != tip:
        try:
            _run(repo, ["update-ref", f"refs/heads/{branch}", tip])
        except Exception:  # noqa: BLE001 — a locked/colliding ref is not fatal
            pass
    return tip


def _changed_paths(repo: Path, base_rev: str, tip: str) -> list[str]:
    res = _run(repo, ["diff", "--name-status", f"{base_rev}..{tip}"])
    out: list[str] = []
    for line in (res.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][0] if parts[0] else ""
        if status in ("A", "M", "D", "R"):
            out.append(parts[-1])
    return out


def _porcelain(repo: Path) -> list[str]:
    res = _run(repo, ["status", "--porcelain"])
    return (res.stdout or "").splitlines()


def _is_tasks_untracked(line: str) -> bool:
    """`.kilo/tasks/` is an untracked scaffold dir that exists in this repo and
    must never count as a dirty tree."""
    if not line.startswith("?? "):
        return False
    p = line[3:].strip()
    return p == ".kilo/tasks" or p.startswith(".kilo/tasks/")


# ---------------------------------------------------------------------------
# Rejection classification (machine-readable, stable across ticks)
# ---------------------------------------------------------------------------

# Every batch-wide rejection is attributable to exactly one of these classes.
# The human `rejected` string stays byte-identical for callers and operators;
# the class is what the Night's Watch streaks on and what the pulse renders.
REJECTION_CLASSES = (
    "dirty-untracked",     # an untracked file outside .court/ blocks every pickup
    "dirty-tracked",       # a tracked file (LEDGER.md or a pickup target) is dirty
    "non-paperwork-commit",  # the batch touches paths outside the .court lane
    "invalid-charter",     # unparseable / bad branch / bad status / bad prereq / bad events log
    "invalid-vigil",       # vigil spec fails the vigils contract
    "charter-tamper",      # immutable charter section altered on a frozen quest
    "decree-invalid",      # decree packet fails decisions.validate_decree
    "unknown",             # deletions, unreadable refs, commit failures, anything else
)

_DIRTY_REASON_RE = re.compile(r"^castle working tree has conflicting dirty path")

_PICKUP_REMEDIES = {
    "dirty-untracked": (
        "untracked file {path} blocks every pickup: commit it on a quest branch "
        "or add it to .gitignore; never `git clean`"
    ),
    "dirty-tracked": (
        "tracked file {path} is dirty: commit or stash it with its owner's consent "
        "(pickup refuses to mix unrelated edits into its commit)"
    ),
    "non-paperwork-commit": (
        "the pleasance batch touched {path}, outside the paperwork lane; resend "
        "only .court/quests, .court/vigils, and .court/decrees"
    ),
    "invalid-charter": (
        "charter {path} failed validation; fix the named field (branch, status, "
        "prerequisites, or events journal) and resend it"
    ),
    "invalid-vigil": (
        "vigil {path} failed the vigil contract; fix the named field and resend it"
    ),
    "charter-tamper": (
        "immutable charter section changed on pleasance for {path}; revert it on "
        "the pleasance branch (a frozen charter's scope text is not amendable)"
    ),
    "decree-invalid": (
        "decree packet {path} failed validation; fix the named field (quest, "
        "ruling, or status) and resend it"
    ),
    "unknown": (
        "pickup rejected for {path}; inspect the reason and the pleasance tip "
        "before resending"
    ),
}


def _is_untracked(path: str, untracked) -> bool:
    """Git collapses a wholly-untracked directory to ``scripts/``; a file path
    inside it is still untracked for classification purposes."""
    path = str(path)
    return any(path == str(u) or (str(u).endswith("/") and path.startswith(str(u)))
               for u in (untracked or ()))


def expand_untracked_dirs(repo: Path, paths, untracked) -> list[str]:
    """Expand git's collapsed untracked-directory entries for diagnostics.

    The human ``rejected`` reason keeps git's own collapse (byte-identical for
    callers); only ``rejection_paths`` names the actual blocking file."""
    listed: Optional[list[str]] = None
    out: list[str] = []
    for raw in paths or ():
        path = str(raw)
        if _is_untracked(path, untracked) and path.endswith("/"):
            if listed is None:
                lines = _run(repo, ["status", "--porcelain=v1", "-uall"]).stdout or ""
                listed = [l[3:].strip() for l in lines.splitlines()
                          if l.startswith("?? ")]
            out.extend(sorted(p for p in listed if p.startswith(path)))
        else:
            out.append(path)
    return out


def classify_rejection(reason: Optional[str], *, paths=(),
                       untracked=()) -> tuple[str, list[str]]:
    """Map a pickup rejection reason to ``(rejection_class, rejection_paths)``.

    ``paths`` is what the site itself knows (the conflicting dirty paths, the
    out-of-lane batch), so path extraction never depends on truncating a human
    string; ``untracked`` separates an untracked blocker from a tracked one.
    """
    text = reason or ""
    if not text:
        return "unknown", []
    named = [str(p) for p in (paths or ())]
    if text.startswith("out-of-lane path"):
        m = re.match(r"out-of-lane path (\S+)", text)
        return "non-paperwork-commit", ([m.group(1)] if m else named)
    if _DIRTY_REASON_RE.match(text):
        if not named:
            m = re.search(r"\((.*)\);", text)
            named = [m.group(1)] if m else []
        untracked_set = {str(p) for p in (untracked or ())}
        cls = ("dirty-untracked" if named and _is_untracked(named[0], untracked_set)
               else "dirty-tracked")
        return cls, named
    m = re.match(r"invalid (?:charter|events log) (\S+):", text)
    if m:
        lowered = text.lower()
        if "tamper" in lowered or "immutable" in lowered:
            return "charter-tamper", [m.group(1)]
        return "invalid-charter", [m.group(1)]
    if "charter" in text.lower() and ("tamper" in text.lower() or "immutable" in text.lower()):
        return "charter-tamper", named
    m = re.match(r"invalid vigil (\S+):", text)
    if m:
        return "invalid-vigil", [m.group(1)]
    m = re.match(r"invalid decree (\S+):", text)
    if m:
        return "decree-invalid", [m.group(1)]
    m = re.match(r"cannot read (\S+) at pleasance tip", text)
    if m:
        return "unknown", [m.group(1)]
    return "unknown", named


def pickup_remedy(rejection_class: str, paths=None) -> str:
    """The single next action that clears a rejection class, in plain words.

    Pure and non-destructive: it names what a human/remote Steward must do and
    never proposes cleaning, resetting, or force-pushing anything."""
    first = next((str(p) for p in (paths or ()) if str(p)), "<unknown path>")
    template = _PICKUP_REMEDIES.get(rejection_class) or _PICKUP_REMEDIES["unknown"]
    return template.format(path=first)


def _append_ledger(repo: Path, line: str) -> None:
    p = Path(repo) / ".court" / "LEDGER.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    p.write_text(text + line, encoding="utf-8")


def _commit(repo: Path, paths: list, msg: str) -> bool:
    return bool(git_ops.git_commit_paths(paths, msg, cwd=repo).get("ok"))


def _push_best_effort(repo: Path, branch: str, say=print) -> None:
    """Push the trunk back to origin. Best-effort: a push failure must never
    fail the pickup — the paperwork is already merged locally and journaled."""
    try:
        res = _run(repo, ["push", "origin", branch])
    except Exception as e:  # noqa: BLE001
        say(f"⚠️  push {branch} to origin failed: {e}")
        return
    if res.returncode != 0:
        say(f"⚠️  push {branch} to origin failed: "
            f"{(res.stderr or res.stdout or '').strip()[:160]}")


def run(repo: Path, *, fetch: bool = True, base: str = "castle",
        branch: str = PICKUP_BRANCH, dry_run: bool = False, say=print) -> dict:
    """Execute one pleasance pickup against the checked-out castle trunk.

    Returns a small verdict dict: ``{"picked": [paths], "rejected": reason}``
    plus either ``"skipped"`` (nothing new) or ``"committed"`` (short sha of
    the castle commit that landed the paperwork) on success. A rejection never
    advances ``applied_tip``; a success does, after one atomic commit.
    """
    repo = Path(repo)
    state = load_state(repo)
    applied_tip = (state.get("applied_tip") or "")

    if fetch:
        ok, msg = fetch_pleasance(repo)
        if not ok:
            say(f"⚠️  fetch {branch}: {msg}")
    tip = _resolve_tip(repo, branch)
    if not tip:
        if not applied_tip:
            say("no pleasance branch on origin; nothing to pick up")
            return {"picked": [], "rejected": None, "rejection_class": None,
                    "rejection_paths": [], "skipped": "no pleasance branch",
                    "decrees": []}
        say("pleasance branch is gone; nothing new to pick up")
        return {"picked": [], "rejected": None, "rejection_class": None,
                "rejection_paths": [], "skipped": "no pleasance branch",
                "decrees": []}
    if applied_tip and applied_tip == tip:
        say(f"pleasance already applied at {tip[:12]}")
        return {"picked": [], "rejected": None, "rejection_class": None,
                "rejection_paths": [], "skipped": "already applied",
                "decrees": []}

    base_rev = applied_tip or base
    if not applied_tip:
        # Diff against the FORK POINT (merge-base), not the castle tip: castle
        # lives far ahead of pleasance's lineage and `diff castle..tip` would
        # surface hundreds of unrelated reverse changes (the 2026-11-04 Q1141
        # false rejection). The lane must only ever see what the remote author
        # actually added.
        mb = _run(repo, ["merge-base", base_rev, tip]).stdout.strip()
        base_rev = mb or base_rev
    elif not _show(repo, f"{applied_tip}^{{commit}}") \
            or not _run(repo, ["merge-base", "--is-ancestor", applied_tip, tip]).returncode == 0:
        # The applied marker no longer sits in the branch's ancestry (a remote
        # rebase happened) — fall back to the fork point rather than erroring.
        mb = _run(repo, ["merge-base", base, tip]).stdout.strip()
        base_rev = mb or base
    revs = _run(repo, ["rev-list", f"{base_rev}..{tip}"]).stdout.split()
    if not revs:
        say("no new pleasance work")
        return {"picked": [], "rejected": None, "rejection_class": None,
                "rejection_paths": [], "skipped": "no new pleasance work",
                "decrees": []}
    changed = _changed_paths(repo, base_rev, tip)

    # Decree packets mutate existing quest charters, so resolve the target
    # charters BEFORE the dirty-tree gate: an uncommitted edit on a charted
    # Quest the ruling is about would otherwise be silently mixed into the
    # pickup commit (or clobbered by it).
    decree_target_paths: set[str] = set()
    for path in changed:
        if not _DECREE_FILE_RE.match(path):
            continue
        text = _content(repo, tip, path)
        if text is None:
            continue
        try:
            packet = decisions.parse_decree_packet(text)
        except decisions.DecreeError:
            continue  # the validation pass below reports it once, precisely
        for item in packet["decisions"]:
            qp = decisions.quest_path(repo, item["quest"])
            if qp is not None:
                try:
                    decree_target_paths.add(qp.relative_to(repo).as_posix())
                except ValueError:
                    pass

    # Global gate 1: the lane is surgical — only charters, vigil specs, decree
    # packets, and the two branch-surface docs (which ride the branch but are
    # never merged).
    reason: Optional[str] = None
    reject_paths: list[str] = []
    untracked_paths: set[str] = set()
    surface_docs: list[str] = []
    for path in changed:
        if _SURFACE_DOC_RE.match(path):
            surface_docs.append(path)
            continue
        if not (_QUEST_CHARTER_RE.match(path) or _VIGIL_FILE_RE.match(path)
                or _DECREE_FILE_RE.match(path)):
            reason = (f"out-of-lane path {path} in pleasance batch — only "
                      f".court/quests/<id>.md, .court/quests/<id>.events.jsonl, "
                      f".court/vigils/<id>.json, .court/decrees/<id>.json, AGENTS.md "
                      f"and PLEASANCE.md may be picked up")
            reject_paths = [path]
            break
    # Global gate 2: never mix the pickup commit with a dirty tree the
    # pickup would collide with. Uncommitted `.court` paperwork written by
    # the live pipeline (charter status flips, vigil stamps on the castle
    # checkout) is normal in production and must not freeze the lane; only a
    # dirty path OUTSIDE `.court/**`, a dirty path the pickup is ABOUT to
    # write, or a dirty `.court/LEDGER.md` (pickup appends to it) refuses.
    if reason is None:
        porcelain = [l for l in _porcelain(repo) if not _is_tasks_untracked(l)]
        dirty_paths = [l.strip().split(None, 1)[-1] for l in porcelain]
        untracked_paths = {l.strip().split(None, 1)[-1] for l in porcelain
                           if l.strip().startswith("??")}
        conflicts = [p for p in dirty_paths
                     if not p.startswith(".court/")
                     or p == ".court/LEDGER.md"
                     or p in changed
                     or p in decree_target_paths]
        if conflicts:
            reason = (f"castle working tree has conflicting dirty path(s) "
                      f"({conflicts[0][:120]}); refusing to mix the pickup commit")
            reject_paths = expand_untracked_dirs(repo, conflicts, untracked_paths)

    charters: list[str] = []
    vigils_to_apply: list[str] = []
    decree_paths: list[str] = []
    decree_packets: dict[str, dict] = {}
    # Per-file supersession, NOT batch failure: a remote author may re-send a
    # charter or vigil that the pipeline already owns (a commissioned quest's
    # charter is frozen; a live vigil is already active). Those files are
    # skipped with a recorded reason so the rest of the batch still lands.
    # Only lane-integrity problems (out-of-lane paths, unreadable/deleted
    # files, malformed paperwork, dirty collisions) reject the whole batch.
    skipped_files: list[dict] = []
    if reason is None:
        for path in changed:
            if _QUEST_CHARTER_RE.match(path):
                text = _content(repo, tip, path)
                if text is None:
                    reason = f"cannot read {path} at pleasance tip (deletion is not a pickup)"
                    reject_paths = [path]
                    break
                if path.endswith(".events.jsonl"):
                    ok, why = validate_events_log(text)
                    if not ok:
                        reason = f"invalid events log {path}: {why}"
                        reject_paths = [path]
                        break
                    charters.append(path)
                    continue
                ok, why = validate_charter(text, repo)
                if not ok:
                    reason = f"invalid charter {path}: {why}"
                    reject_paths = [path]
                    break
                # Freeze rule: the base status on castle is authoritative. A
                # commissioned quest's charter is frozen — a remote LLM must
                # not amend paperwork the pipeline already owns. Skip the file
                # (the castle copy wins) instead of wedging the whole lane.
                qid = Path(path).stem
                base_text = _content(repo, base, path)
                if base_text is not None:
                    try:
                        base_quest = Quest.from_markdown(base_text)
                    except ValueError:
                        base_quest = None
                    if base_quest is not None and (base_quest.status or "").strip().upper() not in _OPEN_STATUSES:
                        skip_why = (f"quest {qid} is commissioned on castle "
                                    f"(status {(base_quest.status or '').strip().upper()}) "
                                    f"and its charter is frozen")
                        skipped_files.append({"path": path, "reason": skip_why})
                        say(f"SKIP {path}: {skip_why} — castle copy kept")
                        continue
                charters.append(path)
            elif _VIGIL_FILE_RE.match(path):
                text = _content(repo, tip, path)
                if text is None:
                    reason = f"cannot read {path} at pleasance tip (deletion is not a pickup)"
                    reject_paths = [path]
                    break
                ok, why = validate_vigil(text, repo)
                if not ok:
                    reason = f"invalid vigil {path}: {why}"
                    reject_paths = [path]
                    break
                vid = Path(path).stem
                base_text = _content(repo, base, path)
                if base_text is not None:
                    try:
                        base_vigil = json.loads(base_text)
                    except ValueError:
                        base_vigil = None
                    if isinstance(base_vigil, dict) and base_vigil.get("status") == "active":
                        skip_why = f"vigil {vid} is already active on castle (duplicate delivery)"
                        skipped_files.append({"path": path, "reason": skip_why})
                        say(f"SKIP {path}: {skip_why} — castle copy kept")
                        continue
                vigils_to_apply.append(path)
            elif _DECREE_FILE_RE.match(path):
                text = _content(repo, tip, path)
                if text is None:
                    reason = f"cannot read {path} at pleasance tip (deletion is not a pickup)"
                    reject_paths = [path]
                    break
                ok, why = decisions.validate_decree(text, repo)
                if not ok:
                    reason = f"invalid decree {path}: {why}"
                    reject_paths = [path]
                    break
                packet = decisions.parse_decree_packet(text)
                if packet["id"] in set(state.get("applied_decrees") or []):
                    skip_why = f"decree {packet['id']} was already applied"
                    skipped_files.append({"path": path, "reason": skip_why})
                    say(f"SKIP {path}: {skip_why} — castle copy kept")
                    continue
                decree_paths.append(path)
                decree_packets[path] = packet

    accepted = charters + vigils_to_apply + decree_paths
    if dry_run:
        for p in accepted:
            extra = (f" ({len(decree_packets[p]['decisions'])} royal decision(s))"
                     if p in decree_packets else "")
            say(f"would accept: {p}{extra}")
        for p in surface_docs:
            say(f"would accept: {p} (branch-surface doc — kept on pleasance, never merged)")
        for item in skipped_files:
            say(f"would skip: {item['path']} — {item['reason']}")
        if reason:
            say(f"REJECT: {reason}")
        r_class, r_paths = (classify_rejection(reason, paths=reject_paths,
                                               untracked=untracked_paths)
                            if reason else (None, []))
        return {"picked": accepted, "rejected": reason,
                "rejection_class": r_class, "rejection_paths": r_paths,
                "dry_run": True,
                "surface_docs": surface_docs, "skipped_files": skipped_files,
                "decrees": []}

    if reason:
        last = state.get("last_attempt") or {}
        if last.get("reason") != reason:
            _append_ledger(repo,
                           f"- **{_iso()}** — pleasance pickup REJECTED tip {tip[:12]}: {reason}\n")
            _commit(repo, [".court/LEDGER.md"], "court: record pleasance pickup rejection")
            _push_best_effort(repo, base, say)
        state["last_attempt"] = {"tip": tip, "reason": reason}
        save_state(repo, state)
        say(f"REJECT: {reason}")
        r_class, r_paths = classify_rejection(reason, paths=reject_paths,
                                              untracked=untracked_paths)
        return {"picked": [], "rejected": reason,
                "rejection_class": r_class, "rejection_paths": r_paths,
                "decrees": []}

    # A batch carrying ONLY branch-surface docs and/or superseded files changes
    # nothing on castle: advance the applied marker so the next scrape diffs
    # cleanly, journal the touch/skip, but never create a trunk commit for a
    # non-.court file.
    if not accepted and (surface_docs or skipped_files):
        ledger_bits = []
        if surface_docs:
            ledger_bits.append(
                f"surface docs updated at {tip[:12]}: {', '.join(surface_docs)} "
                f"(kept on the branch; nothing merged onto castle)")
        if skipped_files:
            detail = "; ".join(f"{i['path']} ({i['reason']})" for i in skipped_files)
            ledger_bits.append(
                f"skipped {len(skipped_files)} superseded file(s) at {tip[:12]}: {detail}")
        _append_ledger(repo, f"- **{_iso()}** — pleasance pickup: {' | '.join(ledger_bits)}\n")
        _commit(repo, [".court/LEDGER.md"], "court: record pleasance pickup skip")
        _push_best_effort(repo, base, say)
        state["applied_tip"] = tip
        state["last_attempt"] = {"tip": tip, "reason": ""}
        save_state(repo, state)
        say(f"✅ pleasance pickup advanced {tip[:12]}: nothing to merge "
            f"({len(skipped_files)} skipped file(s), {len(surface_docs)} surface doc(s))")
        return {"picked": [], "rejected": None, "rejection_class": None,
                "rejection_paths": [], "decrees": [],
                "skipped": ("superseded files only" if skipped_files else "surface docs only"),
                "surface_docs": surface_docs, "skipped_files": skipped_files}

    skip_note = ""
    if skipped_files:
        detail = "; ".join(f"{i['path']} ({i['reason']})" for i in skipped_files)
        skip_note = f" — skipped {len(skipped_files)} superseded: {detail}"
    decree_result = {"applied": [], "paths": [], "decree_ids": []}
    try:
        for p in accepted:
            _run(repo, ["checkout", tip, "--", p])
        if decree_packets:
            decree_result = decisions.apply_packets(
                repo, [decree_packets[p] for p in decree_paths])
        counts = [f"{len(charters)} charter(s)", f"{len(vigils_to_apply)} vigil(s)"]
        if decree_result["applied"]:
            counts.append(f"{len(decree_result['applied'])} decree ruling(s)")
        _append_ledger(
            repo,
            f"- **{_iso()}** — pleasance pickup applied {tip[:12]}: "
            f"{', '.join(counts)}{skip_note}\n",
        )
        quest_files: list[str] = []
        for qp in decree_result.get("paths") or []:
            try:
                rel = qp.relative_to(repo).as_posix()
            except ValueError:
                rel = str(qp)
            if rel not in quest_files:
                quest_files.append(rel)
        commit_paths = accepted + quest_files + [".court/LEDGER.md"]
        commit_subject = (f"court: pleasance pickup {len(charters)} charter(s), "
                          f"{len(vigils_to_apply)} vigil(s)")
        if decree_result["applied"]:
            commit_subject += f", {len(decree_result['applied'])} decree ruling(s)"
        if not _commit(repo, commit_paths, commit_subject):
            raise RuntimeError("git commit failed")
        head = _show(repo, "HEAD")
    except Exception as e:  # noqa: BLE001 — never leave the command half-applied
        try:
            _run(repo, ["reset", "--hard", base], timeout=120)
        except Exception:  # noqa: BLE001
            pass
        say(f"REJECT: pickup could not be committed — {e}; tree restored to {base}")
        return {"picked": [], "rejected": f"commit failed: {e}",
                "rejection_class": "unknown", "rejection_paths": [],
                "decrees": []}

    _push_best_effort(repo, base, say)
    state["applied_tip"] = tip
    state["last_attempt"] = {"tip": tip, "reason": ""}
    if decree_result.get("decree_ids"):
        applied_ids = list(dict.fromkeys(
            list(state.get("applied_decrees") or []) + list(decree_result["decree_ids"])))
        state["applied_decrees"] = applied_ids[-500:]
    save_state(repo, state)
    summary = (f"{len(charters)} charter(s), {len(vigils_to_apply)} vigil(s)")
    if decree_result["applied"]:
        summary += f", {len(decree_result['applied'])} decree ruling(s)"
    say(f"✅ pleasance pickup applied {tip[:12]}: {summary}, {len(skipped_files)} skipped")
    return {"picked": accepted, "rejected": None, "rejection_class": None,
            "rejection_paths": [], "committed": (head or tip)[:12],
            "skipped_files": skipped_files, "decrees": decree_result.get("applied") or []}