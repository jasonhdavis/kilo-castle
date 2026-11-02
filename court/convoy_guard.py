"""Convoy promotion guards: no silent riders, no stale-suite promotions.

Eligibility has always been decided per Quest while promotion is executed per
branch: the Gatekeeper fast-forwards ``castle`` to the convoy tip, so anything
merged onto ``the-gatehouse/<cogship>`` travels into the trunk whether or not it
was part of the pack that collect authorised.

The 2026-11-01 cogship-382 incident is the reference case: Night's Watch issued
``collect Q1009`` at 13:51Z, the Court Artist merged Q1013 onto the same convoy
branch at 13:59-14:00Z for a UI preview, and the Gatekeeper then promoted the
tip — so Q1013's code entered ``castle`` while its charter still read ``GATE``
with the artist review outstanding.

This module makes the promotion unit explicit and checkable:

* the pack that collect authorised is recorded on disk (``.court/convoys``);
* merges into the convoy branch are enumerated from git, not from the board;
* every merged Quest must be part of the pack, in a promotable status, and free
  of a pending UI review;
* the green suite proof must have been stamped at the current convoy tip.

Nothing here mutates git. The command exits non-zero so the Gatekeeper (or a
human) must back the offending branch out instead of promoting it.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

PROMOTABLE_STATUSES = {"GATE", "READY_TO_RAZE"}

_BRANCH_MERGE_RE = re.compile(r"^Merge branch '([^']+)' into (\S+)$")
_QUEST_SEGMENT_RE = re.compile(r"^q(\d+)", re.IGNORECASE)
_COGSHIP_RE = re.compile(r"cogship-(\d+)")
_UI_REVIEW_RE = re.compile(r"^\s*(?:[-*]\s*)?\*{0,2}UI Review\*{0,2}:?\*{0,2}\s*(.*)$", re.IGNORECASE)
_STATUS_RE = re.compile(r"^status:\s*(\S+)", re.MULTILINE)


@dataclass
class Finding:
    """One guard observation. ``error`` blocks promotion; ``warn`` does not."""

    level: str
    message: str
    quest: str = ""
    branch: str = ""

    def render(self) -> str:
        mark = "🔴" if self.level == "error" else "⚠️ "
        return f"{mark} {self.message}"


def normalize_cogship_id(value: str) -> str:
    match = _COGSHIP_RE.search(value or "")
    if match:
        return f"cogship-{int(match.group(1)):03d}"
    return (value or "").strip()


def convoy_branch(cogship_id: str) -> str:
    return f"the-gatehouse/{normalize_cogship_id(cogship_id)}"


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
    )


def convoy_tip(repo_root: Path, branch: str) -> str:
    res = _git(repo_root, "rev-parse", "--verify", f"{branch}^{{commit}}")
    return res.stdout.strip() if res.returncode == 0 else ""


def quest_id_from_branch(branch: str) -> str:
    """`quest/q1025/q1059-purchases-...` -> ``Q1059`` (last segment wins)."""
    segment = (branch or "").rstrip("/").rsplit("/", 1)[-1]
    match = _QUEST_SEGMENT_RE.match(segment)
    return f"Q{int(match.group(1))}" if match else ""


def merged_quest_branches(repo_root: Path, branch: str, base: str = "castle") -> dict[str, list[str]]:
    """Quest branches merged into ``branch`` since ``base``, with their merge commits.

    Only Quest/Scout branches are returned; merges of ``castle`` into the convoy
    (the routine rebase) are ignored.
    """
    merged: dict[str, list[str]] = {}
    res = _git(repo_root, "log", "--merges", "--format=%H%x09%s", f"{base}..{branch}")
    if res.returncode != 0:
        return merged
    for line in res.stdout.splitlines():
        if "\t" not in line:
            continue
        sha, subject = line.split("\t", 1)
        match = _BRANCH_MERGE_RE.match(subject.strip())
        if not match:
            continue
        source = match.group(1)
        if not (source.startswith("quest/") or source.startswith("scout/")):
            continue
        merged.setdefault(source, []).append(sha)
    return merged


def contained_quest_branches(repo_root: Path, branch: str, base: str = "castle") -> dict[str, list[str]]:
    """Quest branches whose tip is already inside ``branch`` and not in ``base``.

    Merge subjects are advisory — `git merge --no-ff -m "preview Q1013"` hides
    the source branch name entirely. Containment is authoritative: if a branch
    tip is reachable from the convoy tip but not from the trunk, that code is on
    the convoy whether or not a merge commit names it.
    """
    found: dict[str, list[str]] = {}
    res = _git(repo_root, "for-each-ref", "--format=%(refname:short)",
               "refs/heads/quest/", "refs/heads/scout/")
    if res.returncode != 0:
        return found
    for name in res.stdout.splitlines():
        name = name.strip()
        if not name:
            continue
        if _git(repo_root, "merge-base", "--is-ancestor", name, branch).returncode != 0:
            continue
        if _git(repo_root, "merge-base", "--is-ancestor", name, base).returncode == 0:
            continue
        tip = _git(repo_root, "rev-parse", name).stdout.strip()
        found[name] = [tip]
    return found


def pack_record_path(repo_root: Path, cogship_id: str) -> Path:
    return Path(repo_root) / ".court" / "convoys" / f"{normalize_cogship_id(cogship_id)}.json"


def record_pack(
    repo_root: Path,
    cogship_id: str,
    quest_ids: Iterable[str],
    base: str = "castle",
    branch: str = "",
    worktree: str = "",
    packed_tip: str = "",
    extra: Optional[dict] = None,
) -> Optional[Path]:
    """Persist the pack collect authorised. This is the promotion allow-list.

    Returns ``None`` when ``repo_root`` is not the active checkout (no
    ``.court/quests`` entries for the given ids). That keeps test harnesses and
    read-only guard runs from writing pack records into an engine checkout.
    """
    ids = [str(q) for q in quest_ids]
    quests_dir = Path(repo_root) / ".court" / "quests"
    if not quests_dir.is_dir():
        return None
    for quest_id in ids:
        stem = quest_id.split("-")[0]
        if not any(quests_dir.glob(f"{stem}*.md")):
            return None
    path = pack_record_path(repo_root, cogship_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cogship_id": normalize_cogship_id(cogship_id),
        "quest_ids": [str(q) for q in quest_ids],
        "base": base,
        "branch": branch or convoy_branch(cogship_id),
        "worktree": worktree,
        "packed_tip": packed_tip,
        "packed_at": _now_iso(),
    }
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_pack(repo_root: Path, cogship_id: str) -> dict:
    try:
        return json.loads(pack_record_path(repo_root, cogship_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _quest_file(repo_root: Path, quest_id: str) -> Optional[Path]:
    directory = Path(repo_root) / ".court" / "quests"
    if not directory.is_dir():
        return None
    for pattern in (f"{quest_id}-*.md", f"{quest_id}*.md"):
        for candidate in sorted(directory.glob(pattern)):
            if candidate.is_file():
                return candidate
    return None


def quest_status(repo_root: Path, quest_id: str) -> str:
    path = _quest_file(repo_root, quest_id)
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    match = _STATUS_RE.search(text)
    return (match.group(1).strip().upper() if match else "")


def ui_review_pending(repo_root: Path, quest_id: str) -> bool:
    """True when the Quest's UI review is still outstanding.

    Prefers the engine's own extraction (it understands the Master of Coin's
    Audit / Tribute Rendered sections and sign-off evidence); falls back to the
    LAST matching ``UI Review:`` line, because reviews are appended and an early
    PENDING note must not outlive a later approval.
    """
    try:
        from . import store

        quest = store.load(quest_id, court_root=Path(repo_root) / ".court")
        status = quest.extract_ui_review_status()
        if status:
            return status.strip().upper().startswith("PENDING")
    except Exception:
        pass
    path = _quest_file(repo_root, quest_id)
    if path is None:
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    last = ""
    for line in text.splitlines():
        match = _UI_REVIEW_RE.match(line)
        if match:
            last = match.group(1)
    return "pending" in last.lower()


_UI_LINE_RE = re.compile(r"(?im)^\s*[-*]?\s*\*{0,2}UI Review\*{0,2}:?\*{0,2}\s*(.+)$")
_SIGNOFF_LINE_RE = re.compile(r"(?im)^\s*[-*]?\s*\*{0,2}UI Sign-off\*{0,2}:?\*{0,2}\s*(.+)$")
_PLACEHOLDER_RE = re.compile(r"(?i)^(pending|tbd|todo|n/a|none|unknown)?[\s.…-]*$")


def ui_assent_state(repo_root: Path, quest_id: str) -> tuple[str, str]:
    """Classify the Quest's UI review as recorded *in writing* in the charter.

    Returns ``(state, detail)`` where state is one of:

    * ``signed``       — an explicit ``**UI Sign-off:** <who> <when>`` line;
    * ``waived``       — ``UI Review: WAIVED …`` (the explicit --skip-ui-review trail);
    * ``not_required`` — ``UI Review: None required`` / ``not required``;
    * ``pending``      — any other outstanding review text;
    * ``missing``      — no UI Review or UI Sign-off record at all.

    An ``APPROVED`` claim with no sign-off line is NOT approval: chat assent is
    authoritative, and this is where it has to become evidence a gate can read.
    """
    path = _quest_file(repo_root, quest_id)
    if path is None:
        return "missing", ""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return "missing", ""

    signoff = ""
    for match in _SIGNOFF_LINE_RE.finditer(text):
        value = match.group(1).strip().strip("*").strip()
        if value and not _PLACEHOLDER_RE.match(value):
            signoff = value
    if signoff:
        return "signed", signoff

    review = ""
    for match in _UI_LINE_RE.finditer(text):
        review = match.group(1).strip().strip("*").strip()
    if not review:
        return "missing", ""

    lowered = review.lower()
    if "waived" in lowered:
        return "waived", review
    if "not required" in lowered or "none required" in lowered or "no ui" in lowered:
        return "not_required", review
    if any(word in lowered for word in ("approved", "verified", "passed")):
        # An APPROVED assertion with no UI Sign-off line: treated as outstanding.
        return "pending", review + " (asserted without a recorded sign-off)"
    return "pending", review


def unpromoted_quests_in_trunk(
    repo_root: Path,
    trunk: str = "castle",
    allowed_statuses: frozenset[str] = frozenset({"READY_TO_RAZE", "DONE"}),
) -> list[Finding]:
    """Trunk invariant: no Quest branch may be merged into ``trunk`` unless its
    charter already records the promotion decision.

    Message-independent (unlike merge-subject parsing), so it catches a rider
    even when the merge commit was written with a custom subject. Quests whose
    charter cannot be located (archived/razed) are skipped: an absent charter is
    not evidence of an undecided promotion.
    """
    findings: list[Finding] = []
    res = _git(repo_root, "for-each-ref", "--format=%(refname:short)", "refs/heads/quest/")
    if res.returncode != 0:
        return findings
    for name in res.stdout.splitlines():
        branch = name.strip()
        if not branch:
            continue
        qid = quest_id_from_branch(branch)
        if not qid:
            continue
        if _git(repo_root, "merge-base", "--is-ancestor", branch, trunk).returncode != 0:
            continue
        status = quest_status(repo_root, qid)
        if status and status not in allowed_statuses:
            findings.append(Finding(
                "error",
                f"{qid} ({branch}) is already merged into {trunk} but its status is {status} — "
                f"code in the trunk with no promotion decision.",
                quest=qid,
                branch=branch,
            ))
    return findings


def suite_proof(repo_root: Path, cogship_id: str) -> dict:
    path = Path(repo_root) / ".court" / "suites" / f"{normalize_cogship_id(cogship_id)}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def audit_promotion(
    repo_root: Path,
    cogship_id: str,
    base: str = "castle",
    expected_quest_ids: Optional[Iterable[str]] = None,
    allow_ui_pending: bool = False,
    allow_stale_suite: bool = False,
    require_suite: bool = False,
) -> list[Finding]:
    """Pre-promotion audit of a convoy branch against ``base``.

    ``expected_quest_ids`` overrides the recorded pack; when neither is present
    the membership check degrades to a warning (legacy convoys predate the pack
    record) while the status and UI checks still run.
    """
    cid = normalize_cogship_id(cogship_id)
    branch = convoy_branch(cid)
    findings: list[Finding] = []

    tip = convoy_tip(repo_root, branch)
    if not tip:
        findings.append(Finding(
            "warn",
            f"{branch} does not exist — nothing to audit "
            f"(a single-Quest convoy promotes from the Quest's own branch)",
        ))
        return findings

    pack = load_pack(repo_root, cid)
    authorised = {q.split("-")[0].upper() for q in (expected_quest_ids or pack.get("quest_ids") or [])}

    # Already promoted: `base..branch` is empty, so the pre-promotion checks have
    # nothing to read. Fall through to the trunk-side rider check instead of
    # reporting a false clean, using the pack allow-list as the authority.
    if _git(repo_root, "merge-base", "--is-ancestor", tip, base).returncode == 0:
        findings.append(Finding(
            "warn",
            f"{branch} is already an ancestor of {base} — this convoy has been promoted; "
            f"auditing the trunk instead (`court verify-manifest --cogship {cid}` is the "
            f"post-promotion command)",
        ))
        findings.extend(riders_in_trunk(repo_root, cid, trunk=base, manifest_quest_ids=authorised or None))
        return findings

    # Union of two detectors: merge subjects name the branch (fast, readable),
    # containment proves it even when a merge was committed with a custom
    # message that hides the source branch.
    merged = merged_quest_branches(repo_root, branch, base=base)
    for source, shas in contained_quest_branches(repo_root, branch, base=base).items():
        merged.setdefault(source, shas)

    for source, shas in sorted(merged.items()):
        qid = quest_id_from_branch(source)
        if not qid:
            findings.append(Finding(
                "warn",
                f"{branch} carries a merge from {source}, which is not a Quest branch "
                f"({len(shas)} merge commit(s))",
                branch=source,
            ))
            continue
        if authorised and qid not in authorised:
            findings.append(Finding(
                "error",
                f"{qid} was merged into {branch} but is not part of the {cid} pack — "
                f"it would ride the fast-forward into {base}. Back {source} out before promoting.",
                quest=qid,
                branch=source,
            ))
            continue
        status = quest_status(repo_root, qid)
        if status and status not in PROMOTABLE_STATUSES:
            findings.append(Finding(
                "error",
                f"{qid} is {status} on the convoy branch — promotion requires "
                f"{'/'.join(sorted(PROMOTABLE_STATUSES))}; back {source} out of the pack.",
                quest=qid,
                branch=source,
            ))
        if not allow_ui_pending and ui_review_pending(repo_root, qid):
            findings.append(Finding(
                "error",
                f"{qid} still has a pending UI review — its code must not enter {base} "
                f"until the artist signs off (or pass --allow-ui-pending deliberately).",
                quest=qid,
                branch=source,
            ))

    if authorised and not merged:
        findings.append(Finding("warn", f"{cid} carries no candidate merges yet — nothing to promote"))

    proof = suite_proof(repo_root, cid)
    proof_head = str(proof.get("head_sha") or proof.get("head") or "").strip()
    if proof and proof_head and proof_head != tip and not allow_stale_suite:
        findings.append(Finding(
            "error",
            f"the green suite proof for {cid} is stale: proof head {proof_head[:12]} != "
            f"convoy tip {tip[:12]} — the convoy moved after the suite ran. "
            f"Re-run `court runsuite --cogship {cid}` before promoting.",
        ))
    if require_suite and not proof:
        findings.append(Finding(
            "error",
            f"no suite proof recorded for {cid} — run `court runsuite --cogship {cid}` first.",
        ))
    return findings


def _stamped_quest_ids(repo_root: Path, cogship_id: str) -> set[str]:
    """Quest ids whose charter records this cogship_id (active + archived)."""
    cid = normalize_cogship_id(cogship_id)
    found: set[str] = set()
    for directory in (Path(repo_root) / ".court" / "quests", Path(repo_root) / ".court" / "archive"):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.md"):
            try:
                head = path.read_text(encoding="utf-8", errors="ignore")[:2000]
            except OSError:
                continue
            match = re.search(r"^cogship_id:\s*(\S+)", head, re.MULTILINE)
            if match and normalize_cogship_id(match.group(1)) == cid:
                found.add(path.name.split("-")[0].upper())
    return found


def riders_in_trunk(
    repo_root: Path,
    cogship_id: str,
    trunk: str = "castle",
    manifest_quest_ids: Optional[Iterable[str]] = None,
    scan: int = 4000,
) -> list[Finding]:
    """Post-promotion detection: Quest merges into this convoy that are already
    ancestors of ``trunk`` but were never part of the manifest."""
    cid = normalize_cogship_id(cogship_id)
    targets = {convoy_branch(cid)}
    # Authority order: the pack record (what collect actually authorised), then
    # the Quests stamped onto this convoy (board truth), then the caller's set.
    # The caller's set may be a filtered ship selection, so it is the weakest
    # signal: a Quest stamped on the convoy but excluded by a --status filter is
    # not a rider.
    pack_ids = {q.split("-")[0].upper() for q in (load_pack(repo_root, cid).get("quest_ids") or [])}
    stamped_ids = _stamped_quest_ids(repo_root, cid)
    manifest = pack_ids or stamped_ids or {q.split("-")[0].upper() for q in (manifest_quest_ids or [])}
    findings: list[Finding] = []
    res = _git(repo_root, "log", trunk, "--merges", f"--max-count={scan}", "--format=%H%x09%s")
    if res.returncode != 0:
        return findings
    seen: set[str] = set()
    for line in res.stdout.splitlines():
        if "\t" not in line:
            continue
        sha, subject = line.split("\t", 1)
        match = _BRANCH_MERGE_RE.match(subject.strip())
        if not match:
            continue
        source, destination = match.group(1), match.group(2)
        if destination not in targets:
            continue
        qid = quest_id_from_branch(source)
        if not qid or qid in seen:
            continue
        seen.add(qid)
        if manifest and qid not in manifest:
            findings.append(Finding(
                "error",
                f"{qid} rode {cid} into {trunk} (merge {sha[:12]}) but is not part of the "
                f"Cog Ship manifest — its code is in the trunk without a promotion decision.",
                quest=qid,
                branch=source,
            ))
    return findings


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
