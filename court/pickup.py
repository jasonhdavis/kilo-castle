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
    journal, or a vigil spec — anything else anywhere rejects the WHOLE batch
    (journaled once);
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

from . import git_ops
from . import nightwatch
from . import vigils
from .models import Quest, validate_branch_name

PICKUP_BRANCH = "pleasance"
STATE_VERSION = 1

# The only paths a pleasance batch may touch. Anything else under .court/
# (LEDGER.md, config, ...) or outside it rejects the whole pickup. A quest's
# sibling `<id>.events.jsonl` (append-only event journal, the project's
# standard charter companion) is in-lane alongside the charter.
_QUEST_CHARTER_RE = re.compile(r"^\.court/quests/[^/]+\.(?:md|events\.jsonl)$")
_VIGIL_FILE_RE = re.compile(r"^\.court/vigils/[^/]+\.json$")
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
            return payload
    except (OSError, ValueError):
        pass
    return {"version": STATE_VERSION, "repo": str(repo),
            "applied_tip": "", "last_attempt": {"tip": "", "reason": ""}}


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
            return {"picked": [], "rejected": None, "skipped": "no pleasance branch"}
        say("pleasance branch is gone; nothing new to pick up")
        return {"picked": [], "rejected": None, "skipped": "no pleasance branch"}
    if applied_tip and applied_tip == tip:
        say(f"pleasance already applied at {tip[:12]}")
        return {"picked": [], "rejected": None, "skipped": "already applied"}

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
        return {"picked": [], "rejected": None, "skipped": "no new pleasance work"}
    changed = _changed_paths(repo, base_rev, tip)

    # Global gate 1: the lane is surgical — only charters and vigil specs.
    reason: Optional[str] = None
    for path in changed:
        if not (_QUEST_CHARTER_RE.match(path) or _VIGIL_FILE_RE.match(path)):
            reason = (f"out-of-lane path {path} in pleasance batch — only "
                      f".court/quests/<id>.md, .court/quests/<id>.events.jsonl and "
                      f".court/vigils/<id>.json may be picked up")
            break
    # Global gate 2: never mix the pickup commit with a dirty tree the
    # pickup would collide with. Uncommitted `.court` paperwork written by
    # the live pipeline (charter status flips, vigil stamps on the castle
    # checkout) is normal in production and must not freeze the lane; only a
    # dirty path OUTSIDE `.court/**`, a dirty path the pickup is ABOUT to
    # write, or a dirty `.court/LEDGER.md` (pickup appends to it) refuses.
    if reason is None:
        dirty_paths = [l.strip().split(None, 1)[-1] for l in _porcelain(repo)
                       if not _is_tasks_untracked(l)]
        conflicts = [p for p in dirty_paths
                     if not p.startswith(".court/")
                     or p == ".court/LEDGER.md"
                     or p in changed]
        if conflicts:
            reason = (f"castle working tree has conflicting dirty path(s) "
                      f"({conflicts[0][:120]}); refusing to mix the pickup commit")

    charters: list[str] = []
    vigils_to_apply: list[str] = []
    if reason is None:
        for path in changed:
            if _QUEST_CHARTER_RE.match(path):
                text = _content(repo, tip, path)
                if text is None:
                    reason = f"cannot read {path} at pleasance tip (deletion is not a pickup)"
                    break
                if path.endswith(".events.jsonl"):
                    ok, why = validate_events_log(text)
                    if not ok:
                        reason = f"invalid events log {path}: {why}"
                        break
                    charters.append(path)
                    continue
                ok, why = validate_charter(text, repo)
                if not ok:
                    reason = f"invalid charter {path}: {why}"
                    break
                # Freeze rule: the base status on castle is authoritative. A
                # commissioned quest's charter is frozen — a remote LLM must
                # not amend paperwork the pipeline already owns.
                qid = Path(path).stem
                base_text = _content(repo, base, path)
                if base_text is not None:
                    try:
                        base_quest = Quest.from_markdown(base_text)
                    except ValueError:
                        base_quest = None
                    if base_quest is not None and (base_quest.status or "").strip().upper() not in _OPEN_STATUSES:
                        reason = f"quest {qid} is commissioned on castle and its charter is frozen"
                        break
                charters.append(path)
            elif _VIGIL_FILE_RE.match(path):
                text = _content(repo, tip, path)
                if text is None:
                    reason = f"cannot read {path} at pleasance tip (deletion is not a pickup)"
                    break
                ok, why = validate_vigil(text, repo)
                if not ok:
                    reason = f"invalid vigil {path}: {why}"
                    break
                vid = Path(path).stem
                base_text = _content(repo, base, path)
                if base_text is not None:
                    try:
                        base_vigil = json.loads(base_text)
                    except ValueError:
                        base_vigil = None
                    if isinstance(base_vigil, dict) and base_vigil.get("status") == "active":
                        reason = f"vigil {vid} is already active on castle"
                        break
                vigils_to_apply.append(path)

    accepted = charters + vigils_to_apply
    if dry_run:
        for p in accepted:
            say(f"would accept: {p}")
        if reason:
            say(f"REJECT: {reason}")
        return {"picked": accepted, "rejected": reason, "dry_run": True}

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
        return {"picked": [], "rejected": reason}

    try:
        for p in accepted:
            _run(repo, ["checkout", tip, "--", p])
        _append_ledger(
            repo,
            f"- **{_iso()}** — pleasance pickup applied {tip[:12]}: "
            f"{len(charters)} charter(s), {len(vigils_to_apply)} vigil(s)\n",
        )
        commit_paths = accepted + [".court/LEDGER.md"]
        if not _commit(repo, commit_paths,
                       f"court: pleasance pickup {len(charters)} charter(s), {len(vigils_to_apply)} vigil(s)"):
            raise RuntimeError("git commit failed")
        head = _show(repo, "HEAD")
    except Exception as e:  # noqa: BLE001 — never leave the command half-applied
        try:
            _run(repo, ["reset", "--hard", base], timeout=120)
        except Exception:  # noqa: BLE001
            pass
        say(f"REJECT: pickup could not be committed — {e}; tree restored to {base}")
        return {"picked": [], "rejected": f"commit failed: {e}"}

    _push_best_effort(repo, base, say)
    state["applied_tip"] = tip
    state["last_attempt"] = {"tip": tip, "reason": ""}
    save_state(repo, state)
    say(f"✅ pleasance pickup applied {tip[:12]}: {len(charters)} charter(s), {len(vigils_to_apply)} vigil(s)")
    return {"picked": accepted, "rejected": None, "committed": (head or tip)[:12]}