"""Standing Artist Atelier — one merged review workspace, per-Quest patronage
pins, deferred mechanical extraction.

The problem this replaces. UI review used to be three lifecycles (`court
artist` per Quest, `court studio` per cohort, `court atelier` per convoy),
each cutting its own worktree/branch and its own dev server, each torn down on
its own schedule. Two failure classes followed: (1) stale studios — a cohort
worktree that was never `--close`d lingered forever and kept rendering in the
sidebar, and `convoy_guard.studio_pending` then silently blocked that Quest's
whole lane; (2) the interactive "peel" — extracting one Quest's approved UI out
of a shared studio branch while other Quests kept merging in under the
operator's feet.

The model here. There is ONE standing atelier: a single long-lived worktree
(``.kilo/worktrees/artist-atelier``) on a stable branch (``artist/atelier``),
with one merged dev server. Quests merge in as their tribute lands. M'Lord
reviews them in that one merged view and *patronizes* each independently.

The key primitive is the **patronage pin**. When a Quest is patronized, the
engine records the ordered set of that Quest's own artist-review commit SHAs
(``style(...): <QID> royal review`` subject and/or ``Addendum-Quests:`` trailer)
into the atelier manifest — a self-contained record (hash + subject + files),
so it survives the atelier tip moving, rebasing, or new Quests merging in.

Because the pin is a SHA *set* and not "the atelier tip", it never drags in
another Quest's un-accepted, in-progress work: extraction cherry-picks only the
pinned commits' deltas onto that Quest's own branch. Other Quests' commits are
never selected. Overlap on a shared file is a genuine union (routed to the
artist), never silent contamination.

Extraction is therefore **deferred and mechanical**: it happens at collect time
(the engine, deterministically), not interactively under M'Lord's feet. The
Gatekeeper then gates the extracted per-Quest convoy exactly as it gates any
other — it verifies the extracted branch matches the pin, it does not decide
what the pin is.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import git_ops
from .studio_close import (
    applied_patch_ids,
    cherry_pick_labeled,
    labeled_commits,
    worktree_state,
)

# The single standing atelier. A stable name — never cohort-encoded — so there
# is exactly one worktree/branch/server to reason about.
ATELIER_BRANCH = "artist/atelier"
ATELIER_WORKTREE_NAME = "artist-atelier"

MANIFEST_DIR = "atelier"
MANIFEST_FILE = "manifest.json"

# Commit-label grammar (verbatim from studio_close): an artist polish commit
# names the Quest either in a `style(...): <QID> ... royal review` subject or
# an `Addendum-Quests: Q1,Q2` trailer.
_LABELED_SUBJECT = re.compile(r"^style\(.*\):\s*.*\b(?P<qid>q\d+)\b.*royal review", re.IGNORECASE)
_ADDENDUM_TRAILER = re.compile(r"^Addendum-Quests:\s*(?P<list>.+)$", re.MULTILINE | re.IGNORECASE)

# Pinned-SHA durability: a pin is a set of immutable git objects. If the
# atelier branch is rebased, the SHAs still resolve from the object store until
# a gc prunes them, and cherry-pick needs only that the object exists.
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_atelier_branch(branch: str) -> bool:
    return (branch or "").strip() == ATELIER_BRANCH


def worktree_path(repo_root: Path) -> Path:
    return Path(repo_root) / ".kilo" / "worktrees" / ATELIER_WORKTREE_NAME


def manifest_path(repo_root: Path) -> Path:
    return Path(repo_root) / ".court" / MANIFEST_DIR / MANIFEST_FILE


# ---------------------------------------------------------------------------
# Manifest storage
# ---------------------------------------------------------------------------

def _default_manifest() -> dict:
    return {
        "version": 1,
        "branch": ATELIER_BRANCH,
        "worktree": f".kilo/worktrees/{ATELIER_WORKTREE_NAME}",
        "created_at": now_iso(),
        "quests": {},
    }


def load_manifest(repo_root: Path) -> dict:
    p = manifest_path(repo_root)
    if not p.is_file():
        return _default_manifest()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _default_manifest()
    if not isinstance(data, dict):
        return _default_manifest()
    data.setdefault("version", 1)
    data.setdefault("branch", ATELIER_BRANCH)
    data.setdefault("quests", {})
    if not isinstance(data.get("quests"), dict):
        data["quests"] = {}
    return data


def save_manifest(repo_root: Path, manifest: dict, auto_commit: bool = True,
                  commit_msg: Optional[str] = None) -> Path:
    p = manifest_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    if auto_commit:
        try:
            git_ops.git_commit_paths([p], commit_msg or "court: atelier manifest", cwd=Path(repo_root))
        except Exception:
            pass
    return p


def members(repo_root: Path) -> dict:
    return load_manifest(repo_root).get("quests", {})


def member(repo_root: Path, quest_id: str) -> Optional[dict]:
    return members(repo_root).get(quest_id)


def record_member(repo_root: Path, quest, *, merged_at: Optional[str] = None,
                  auto_commit: bool = True) -> dict:
    """Record (or refresh) that a Quest is merged into the standing atelier.

    Membership is independent of patronage: a Quest can be in the atelier
    (merged, under review) long before it is patronized.
    """
    m = load_manifest(repo_root)
    rec = m["quests"].get(quest.id) or {}
    rec.setdefault("merged_at", merged_at or now_iso())
    rec["branch"] = getattr(quest, "branch", "") or rec.get("branch", "")
    rec["title"] = getattr(quest, "title", "") or rec.get("title", "")
    rec.setdefault("patronage", None)
    m["quests"][quest.id] = rec
    save_manifest(repo_root, m, auto_commit=auto_commit,
                  commit_msg=f"court: atelier member {quest.id}")
    return rec


# ---------------------------------------------------------------------------
# Patronage pins
# ---------------------------------------------------------------------------

def pin_for(repo_root: Path, quest_id: str) -> Optional[dict]:
    rec = member(repo_root, quest_id)
    if not rec:
        return None
    pin = rec.get("patronage")
    return pin if isinstance(pin, dict) else None


def _commits_for_shas(repo_root: Path, shas: list[str]) -> list[dict]:
    """Resolve explicit SHAs to self-contained commit records (hash + subject +
    files) so the pin does not depend on the branch tip still existing."""
    out = []
    for sha in shas:
        sha = (sha or "").strip()
        if not _SHA_RE.match(sha):
            continue
        exists = git_ops._run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], Path(repo_root))
        if not exists.get("ok"):
            continue
        subject = git_ops._run(["git", "log", "-1", "--format=%s", sha], Path(repo_root)).get("stdout", "")
        files = [
            ln.strip()
            for ln in git_ops._run(
                ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", sha], Path(repo_root)
            ).get("stdout", "").splitlines()
            if ln.strip()
        ]
        out.append({"hash": sha, "subject": subject, "files": files})
    return out


def labeled_shas(repo_root: Path, atelier_branch: str, quest_id: str,
                 base: str = "castle") -> list[dict]:
    """The Quest's own artist-review commits on the atelier branch, oldest-first,
    as ``{hash, subject, files}`` records."""
    commits = labeled_commits(repo_root, atelier_branch, [quest_id], base).get(quest_id, [])
    return commits


def pin_quest(repo_root: Path, quest, commits: list[dict], *, by: str, note: str = "",
              base: str = "castle", atelier_branch: str = ATELIER_BRANCH,
              auto_commit: bool = True) -> dict:
    """Record M'Lord's patronage: the ordered SHA set that constitutes this
    Quest's approved revision. Never references the atelier tip."""
    m = load_manifest(repo_root)
    rec = m["quests"].get(quest.id) or {}
    rec.setdefault("merged_at", now_iso())
    rec["branch"] = getattr(quest, "branch", "") or rec.get("branch", "")
    rec["title"] = getattr(quest, "title", "") or rec.get("title", "")
    pin = {
        "shas": [c["hash"] for c in commits],
        "commits": [
            {"hash": c["hash"], "subject": c.get("subject", ""), "files": c.get("files", [])}
            for c in commits
        ],
        "by": by,
        "note": note,
        "approved_at": now_iso(),
        "atelier_branch": atelier_branch,
        "base": base,
        "empty": not commits,
        "extracted": not commits,  # nothing to extract when the artist changed nothing
        "extracted_at": now_iso() if not commits else "",
        "extracted_hashes": [],
    }
    rec["patronage"] = pin
    m["quests"][quest.id] = rec
    save_manifest(repo_root, m, auto_commit=auto_commit,
                  commit_msg=f"court: patronize {quest.id}")
    return pin


def mark_extracted(repo_root: Path, quest_id: str, extracted_hashes: list[str],
                   auto_commit: bool = True) -> Optional[dict]:
    m = load_manifest(repo_root)
    rec = m["quests"].get(quest_id)
    if not rec or not isinstance(rec.get("patronage"), dict):
        return None
    rec["patronage"]["extracted"] = True
    rec["patronage"]["extracted_at"] = now_iso()
    rec["patronage"]["extracted_hashes"] = list(extracted_hashes)
    m["quests"][quest_id] = rec
    save_manifest(repo_root, m, auto_commit=auto_commit,
                  commit_msg=f"court: atelier extract {quest_id}")
    return rec["patronage"]


def pending_extraction(repo_root: Path) -> list[str]:
    """Quest ids with a pin whose approved commits are not yet on their branch."""
    out = []
    for qid, rec in members(repo_root).items():
        pin = rec.get("patronage")
        if isinstance(pin, dict) and not pin.get("extracted"):
            out.append(qid)
    return sorted(out)


# ---------------------------------------------------------------------------
# Deferred mechanical extraction
# ---------------------------------------------------------------------------

def extract_quest(repo_root: Path, quest, pin: dict, *,
                  atelier_branch: Optional[str] = None, base: str = "castle",
                  mark: bool = True, auto_commit: bool = True) -> dict:
    """Replay a patronized Quest's pinned commits onto its OWN branch, then
    verify the pin is fully applied. Mechanical: no union, no force, no model.

    Returns a report dict with a ``status`` of ``extracted``, ``already-present``,
    ``empty``, ``no-worktree``, ``dirty``, ``missing-commit`` or ``conflict``.
    On ``conflict`` the worktree is restored and the caller routes the Quest to
    the artist (a genuine code overlap with another merged Quest).
    """
    atelier_branch = atelier_branch or (pin.get("atelier_branch") or ATELIER_BRANCH)
    commits = pin.get("commits") or []
    if not commits:
        if mark:
            mark_extracted(repo_root, quest.id, [], auto_commit=auto_commit)
        return {"status": "empty", "picked": [], "quest": quest.id}

    # Every pinned object must still resolve (a rebased-and-gc'd atelier would
    # orphan the pin; fail closed rather than silently ship less).
    missing = [c["hash"] for c in commits
               if not git_ops._run(["git", "cat-file", "-e", f"{c['hash']}^{{commit}}"],
                                   Path(repo_root)).get("ok")]
    if missing:
        return {"status": "missing-commit", "quest": quest.id, "missing": missing}

    ws = worktree_state(repo_root, getattr(quest, "worktree", "") or "")
    if not ws.get("exists"):
        return {"status": "no-worktree", "quest": quest.id,
                "detail": f"no worktree at '{getattr(quest, 'worktree', '') or '-'}' to extract into"}
    if not ws.get("clean"):
        return {"status": "dirty", "quest": quest.id,
                "detail": f"dirty worktree — commit or stash first ({ws.get('dirty', '')[:120]})"}

    # Idempotence: already fully applied -> verified no-op.
    applied = applied_patch_ids(repo_root, quest.branch, atelier_branch)
    if all(c["hash"] in applied for c in commits):
        if mark:
            mark_extracted(repo_root, quest.id, [c["hash"] for c in commits], auto_commit=auto_commit)
        return {"status": "already-present", "quest": quest.id,
                "picked": [{"source": c["hash"], "new_hash": ""} for c in commits]}

    pick = cherry_pick_labeled(repo_root, quest.worktree, commits)
    if pick["status"] == "conflict":
        return {"status": "conflict", "quest": quest.id,
                "conflicts": pick.get("conflicts") or [],
                "reason": pick.get("reason", ""),
                "picked": pick.get("picked") or []}

    extracted = [p["source"] for p in pick["picked"]]
    if mark:
        mark_extracted(repo_root, quest.id, extracted, auto_commit=auto_commit)
    return {"status": "extracted", "quest": quest.id, "picked": pick["picked"]}


def extraction_status(repo_root: Path, quest) -> dict:
    """Read-only: is this Quest's pin fully applied on its own branch?"""
    pin = pin_for(repo_root, quest.id)
    if not pin:
        return {"pinned": False, "extracted": False, "applied": False}
    if pin.get("empty"):
        return {"pinned": True, "extracted": True, "applied": True}
    atelier_branch = pin.get("atelier_branch") or ATELIER_BRANCH
    applied = applied_patch_ids(repo_root, quest.branch, atelier_branch)
    ok = all(c["hash"] in applied for c in (pin.get("commits") or []))
    return {"pinned": True, "extracted": bool(pin.get("extracted")), "applied": ok}
