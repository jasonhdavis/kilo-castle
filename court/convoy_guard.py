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

Reversibility (gatehouse peel): a Quest can be withdrawn from the trunk after
promotion. ``court peel`` reverts the Quest's promotion commit, records the
withdrawal in ``.court/peels/<QUEST>.json``, and supersedes the convoy's suite
proof. This module reads those records so the trunk invariant treats a peeled
Quest as withdrawn instead of "code in the trunk with no promotion decision".
``court collect`` clears the record when the Quest is packed again — peeling off
and re-packing.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from . import git_ops

PROMOTABLE_STATUSES = {"GATE", "READY_TO_RAZE"}

_BRANCH_MERGE_RE = re.compile(r"^Merge branch '([^']+)' into (\S+)$")
_QUEST_SEGMENT_RE = re.compile(r"^q(\d+)", re.IGNORECASE)
_COGSHIP_RE = re.compile(r"cogship-(\d+)")
_UI_REVIEW_RE = re.compile(r"^\s*(?:[-*]\s*)?\*{0,2}UI Review\*{0,2}:?\*{0,2}\s*(.*)$", re.IGNORECASE)
_STATUS_RE = re.compile(r"^status:\s*(\S+)", re.MULTILINE)


@dataclass
class Finding:
    """One guard observation. ``error`` blocks promotion; ``warn`` does not.

    ``code`` is an optional stable label for programmatic/UI distinction
    (empty for the pre-existing findings, whose messages are unchanged).
    """

    level: str
    message: str
    quest: str = ""
    branch: str = ""
    code: str = ""

    def render(self) -> str:
        mark = "🔴" if self.level == "error" else "⚠️ "
        label = f"[{self.code}] " if self.code else ""
        return f"{mark} {label}{self.message}"


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
    # Re-packing reinstates a previously peeled Quest: its withdrawal record is
    # cleared so the trunk invariant applies to it again.
    for quest_id in ids:
        clear_peel_record(repo_root, quest_id)
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


# ---------------------------------------------------------------------------
# Peel records (gatehouse reversibility)
# ---------------------------------------------------------------------------

def peel_record_path(repo_root: Path, quest_id: str) -> Path:
    """``.court/peels/<QID>.json`` — one file per withdrawn Quest."""
    stem = (quest_id or "").split("-")[0].upper()
    return Path(repo_root) / ".court" / "peels" / f"{stem}.json"


def load_peel_record(repo_root: Path, quest_id: str) -> dict:
    try:
        payload = json.loads(peel_record_path(repo_root, quest_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def clear_peel_record(repo_root: Path, quest_id: str) -> bool:
    """Drop the withdrawal record (the Quest is being packed again).

    Re-packing is the reinstatement signal: a Quest that re-enters a convoy is
    no longer withdrawn, so the trunk invariant applies to it again.
    """
    path = peel_record_path(repo_root, quest_id)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def peeled_quest_ids(repo_root: Path, trunk: str = "castle") -> set[str]:
    """Quest ids whose withdrawal is real: the peel's revert is in the trunk.

    A record whose revert commit is not an ancestor of ``trunk`` is not evidence
    the code is gone (it may have been undone by a later merge), so it does not
    excuse the Quest from the trunk invariant.
    """
    directory = Path(repo_root) / ".court" / "peels"
    if not directory.is_dir():
        return set()
    peeled: set[str] = set()
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        qid = str(payload.get("quest") or path.stem).split("-")[0].upper()
        revert_sha = str(payload.get("revert_sha") or "").strip()
        if not qid or not revert_sha:
            continue
        if _git(repo_root, "merge-base", "--is-ancestor", revert_sha, trunk).returncode == 0:
            peeled.add(qid)
    return peeled


def _quest_file(repo_root: Path, quest_id: str, branch: str = "") -> Optional[Path]:
    directory = Path(repo_root) / ".court" / "quests"
    if not directory.is_dir():
        return None
    # A short Quest id (Q1307) can collide across apps (Marketplace vs Common).
    # When the branch slug is supplied, prefer the file whose stem matches it so
    # the invariant audits the Quest the branch actually belongs to.
    if branch:
        slug = branch.rstrip("/").rsplit("/", 1)[-1]
        for candidate in sorted(directory.glob(f"{quest_id}-*.md")):
            if candidate.is_file() and candidate.stem.lower() == slug.lower():
                return candidate
    for pattern in (f"{quest_id}-*.md", f"{quest_id}*.md"):
        for candidate in sorted(directory.glob(pattern)):
            if candidate.is_file():
                return candidate
    return None


def quest_status(repo_root: Path, quest_id: str, branch: str = "") -> str:
    path = _quest_file(repo_root, quest_id, branch=branch)
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    match = _STATUS_RE.search(text)
    return (match.group(1).strip().upper() if match else "")


#: The UI review states that mean the rendered change has not been through the
#: artist review lane yet. PENDING (including an ``APPROVED`` claim with no
#: recorded sign-off) and DEFERRED both count: a deferral records that the
#: review is still owed, it does not record that it happened.
UI_REVIEW_OUTSTANDING_STATES = ("pending", "deferred")

#: Where a UI-review-outstanding Quest is supposed to go instead of a convoy.
#: Named once so every message points at the same destination.
UI_REVIEW_ROUTE_HINT = (
    "route it to the standing atelier (`court atelier <quest>` merges it for "
    "review, and the watch's atelier lane does it automatically)"
)


def ui_review_state(ui_status: str) -> str:
    """Classify a UI Review status string into one of four states.

    ``pending``   — PENDING, or an APPROVED claim that carries no sign-off;
    ``deferred``  — the review/sign-off text carries ``defer``;
    ``clear``     — reviewed, waived, or explicitly not required;
    ``missing``   — no UI review status recorded at all.

    This is the ONE classifier the packing decisions read. The Night's Watch
    planner, ``court collect``, and the convoy promotion guard all resolve UI
    review through ``ui_review_outstanding`` / ``ui_review_outstanding_for``
    so their answers cannot drift apart (the drift that let Q1480/Q1559/Q887
    ride into cogship-626 while the planner called them not collectable).
    """
    text = (ui_status or "").strip()
    if not text:
        return "missing"
    upper = text.upper()
    lowered = text.lower()
    # A deferral is checked BEFORE the PENDING prefix: "PENDING — deferred while
    # M'Lord is away" is a deferral, not a plain pending review. This keeps the
    # classifier in step with ``ui_assent_state``, which has always read that
    # string as ``deferred``; two classifiers disagreeing about the same text is
    # exactly the drift this shared predicate exists to prevent.
    if upper.startswith("DEFERRED") or "defer" in lowered:
        return "deferred"
    if upper.startswith("PENDING"):
        return "pending"
    return "clear"


def ui_review_outstanding(ui_status: str) -> bool:
    """True when this UI Review status means the review is still outstanding."""
    return ui_review_state(ui_status) in UI_REVIEW_OUTSTANDING_STATES


def ui_review_status_text(repo_root: Path, quest_id: str) -> str:
    """The Quest's UI Review status text, engine-first with a charter fallback.

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
            return status
    except Exception:
        pass
    path = _quest_file(repo_root, quest_id)
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    last = ""
    for line in text.splitlines():
        match = _UI_REVIEW_RE.match(line)
        if match:
            last = match.group(1)
    return last.strip().strip("*").strip()


def ui_review_outstanding_for(repo_root: Path, quest_id: str) -> tuple[bool, str]:
    """``(outstanding, status_text)`` for one Quest's recorded UI review.

    This is the shared entry point for the three packing paths: ``court
    collect`` excludes a candidate when this returns True, the Night's Watch
    planner declares it not collectable for the same reason, and ``court
    verify-convoy`` refuses to promote a convoy that carries one.
    """
    status = ui_review_status_text(repo_root, quest_id)
    return ui_review_outstanding(status), status


def ui_review_pending(repo_root: Path, quest_id: str) -> bool:
    """True when the Quest's UI review is PENDING specifically.

    Kept for callers that need the narrower question (the awaiting board's
    "ui" bucket). Packing decisions use ``ui_review_outstanding_for`` because
    DEFERRED is equally unreviewed.
    """
    return ui_review_state(ui_review_status_text(repo_root, quest_id)) == "pending"


_UI_LINE_RE = re.compile(r"(?im)^\s*[-*]?\s*\*{0,2}UI Review\*{0,2}:?\*{0,2}\s*(.+)$")
_SIGNOFF_LINE_RE = re.compile(r"(?im)^\s*[-*]?\s*\*{0,2}UI Sign-off\*{0,2}:?\*{0,2}\s*(.+)$")
_PLACEHOLDER_RE = re.compile(r"(?i)^(pending|tbd|todo|n/a|none|unknown)?[\s.…-]*$")
_STUDIO_BRANCH_RE = re.compile(r"^artist/.+-ui-studio$")


def ui_assent_state(repo_root: Path, quest_id: str) -> tuple[str, str]:
    """Classify the Quest's UI review as recorded *in writing* in the charter.

    Returns ``(state, detail)`` where state is one of:

    * ``signed``       — an explicit ``**UI Sign-off:** <who> <when>`` line;
    * ``deferred``     — the review or sign-off text carries ``defer`` (tracked debt,
                         closed later by a Court Artist pass);
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
        if "defer" in signoff.lower():
            return "deferred", signoff
        return "signed", signoff

    review = ""
    for match in _UI_LINE_RE.finditer(text):
        review = match.group(1).strip().strip("*").strip()
    if not review:
        return "missing", ""

    lowered = review.lower()
    if "defer" in lowered:
        return "deferred", review
    if "waived" in lowered:
        return "waived", review
    if ("not required" in lowered or "none required" in lowered or "no ui" in lowered
            or "none outstanding" in lowered or "none otherwise" in lowered):
        return "not_required", review
    if any(word in lowered for word in ("approved", "verified", "passed")):
        # An APPROVED assertion with no UI Sign-off line: treated as outstanding.
        return "pending", review + " (asserted without a recorded sign-off)"
    return "pending", review


_UI_MANDATE_LINE_RE = re.compile(
    r"(?im)^\s*[-*]?\s*\*{0,2}UI Mandate\*{0,2}:?\*{0,2}\s*(.+)$"
)
_UI_MANDATE_OFF_RE = re.compile(r"(?i)^\s*(?:none|no|not required|false)\b")
# A mandate as the paperwork actually states it: ONE sentence that names a UI
# session AND demands it. Bounded to the Quest's own charter (Charter Notes,
# Audience Log, decree rulings copied in by pickup) so a sentence about some
# other Quest can never gate this one.
_UI_MANDATE_SENTENCE_RE = re.compile(
    r"(?i)(?:mandatory|explicitly requires|requires|required|required step)"
    r"[^.\n]{0,180}(?:UI Studio|Court Artist)"
    r"|(?:UI Studio|Court Artist)[^.\n]{0,180}(?:mandatory|explicitly requires|is required)"
)


def ui_mandate(repo_root: Path, quest_id: str) -> tuple[bool, str]:
    """True when the Quest's own paperwork REQUIRES a UI review/artist session.

    An outstanding UI review already blocks packing: ``court collect`` refuses
    the candidate and routes it to the standing atelier, and the promotion guard
    refuses a convoy carrying one (code ``ui-review-pending``). A *mandate* is a
    different, stricter requirement — a royal decree or charter note that names
    the session as a required step before the UI may be called complete. A
    mandate cannot be overridden by ``--allow-ui-pending``: downgrading it to
    warning-only debt is how Q1135 shipped with no studio session at all. Only a
    written sign-off, an explicit waiver, or a recorded clearance satisfies it.

    Sources, in order:

    * an explicit ``**UI Mandate:** REQUIRED — <source>`` line (``NONE`` clears);
    * a mandate sentence in the Quest's own charter naming a UI Studio / Court
      Artist session as mandatory or explicitly required.

    Returns ``(mandated, source_text)``. Whether the mandate is SATISFIED is a
    separate question — see ``ui_mandate_outstanding``.
    """
    path = _quest_file(repo_root, quest_id)
    if path is None:
        return False, ""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False, ""

    explicit = ""
    for match in _UI_MANDATE_LINE_RE.finditer(text):
        explicit = match.group(1).strip().strip("*").strip()
    if explicit:
        if _UI_MANDATE_OFF_RE.match(explicit):
            return False, explicit
        return True, explicit

    for match in _UI_MANDATE_SENTENCE_RE.finditer(text):
        sentence = match.group(0).strip()
        return True, " ".join(sentence.split())[:240]
    return False, ""


def ui_mandate_outstanding(repo_root: Path, quest_id: str) -> tuple[bool, str]:
    """True when a UI mandate is recorded and NOT satisfied by written assent.

    ``deferred`` does NOT satisfy a mandate: a deferral is the tracked-debt
    escape for ordinary review, not for a step the owner required before the UI
    could be called complete (Q1135 was shipped exactly that way).
    """
    mandated, source = ui_mandate(repo_root, quest_id)
    if not mandated:
        return False, ""
    state, detail = ui_assent_state(repo_root, quest_id)
    if state in ("signed", "waived", "not_required"):
        return False, ""
    return True, f"{state}: {detail or 'no UI record in the charter'} — mandate: {source}"


def studio_branches(repo_root: Path) -> list[str]:
    """Combined artist studio branches currently defined in the repo.

    Only ``artist/<cohort>-ui-studio`` refs count. Atelier convoys live on
    ``the-gatehouse/*`` and are deliberately UI-review lanes, not studios.
    """
    res = _git(repo_root, "for-each-ref", "--format=%(refname:short)", "refs/heads/artist/")
    if res.returncode != 0:
        return []
    return [
        line.strip()
        for line in res.stdout.splitlines()
        if line.strip() and _STUDIO_BRANCH_RE.match(line.strip())
    ]


def studio_pending(repo_root: Path, quest_id: str) -> tuple[bool, str]:
    """True when the Quest's branch is still inside an OPEN combined studio.

    The failure this closes: a Quest routed to ``court studio`` keeps its base
    code on its own branch while the reviewed artwork lives only on
    ``artist/<cohort>-ui-studio``. Packing the Quest branch before sync-back (or
    ``--close`` extraction) ships pre-review code and silently drops the
    approved studio work — then the studio branch is torn down and that work is
    unrecoverable.

    A studio no longer holds the Quest once any of these is true:

    * the studio tip is already an ancestor of the Quest branch (``--sync-back``
      merged the reviewed state);
    * a dated ``studio sign-off`` ledger stamp exists (``--close`` extraction);
    * the charter carries written UI assent (signed / waived / deferred /
      none-required), so there is no pending review left to lose.
    """
    try:
        from . import store, studio_close
    except Exception:  # pragma: no cover - import cycle safety
        return False, ""
    try:
        quest = store.load(quest_id, court_root=Path(repo_root) / ".court")
    except Exception:
        return False, ""
    branch = (getattr(quest, "branch", "") or "").strip()
    if not branch:
        return False, ""
    quest_tip = convoy_tip(repo_root, branch)
    if not quest_tip:
        return False, ""

    state, _detail = ui_assent_state(repo_root, quest_id)
    if state in ("signed", "waived", "not_required", "deferred"):
        return False, ""

    for studio in studio_branches(repo_root):
        studio_tip = convoy_tip(repo_root, studio)
        if not studio_tip:
            continue
        if _git(repo_root, "merge-base", "--is-ancestor", quest_tip, studio_tip).returncode != 0:
            continue
        if _git(repo_root, "merge-base", "--is-ancestor", studio_tip, quest_tip).returncode == 0:
            continue  # reviewed state already merged into the Quest branch
        try:
            if studio_close.signoff_line(quest):
                continue  # closed via cherry-pick extraction
        except Exception:
            pass
        return True, f"branch {branch} is still merged into open studio {studio}"

    return False, ""


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
    not evidence of an undecided promotion. Quests with a valid peel record are
    skipped too: ``court peel`` reverted their code out of the trunk, so there is
    no undecided code left to flag.
    """
    findings: list[Finding] = []
    peeled = peeled_quest_ids(repo_root, trunk)
    res = _git(repo_root, "for-each-ref", "--format=%(refname:short)", "refs/heads/quest/")
    if res.returncode != 0:
        return findings
    for name in res.stdout.splitlines():
        branch = name.strip()
        if not branch:
            continue
        qid = quest_id_from_branch(branch)
        if not qid or qid in peeled:
            continue
        if _git(repo_root, "merge-base", "--is-ancestor", branch, trunk).returncode != 0:
            continue
        # A branch whose tip is itself a trunk commit is a pointer that was cut
        # from the trunk and never advanced — it carries no Quest code of its
        # own (a freshly dispatched Quest before its first commit, or an OPEN
        # Quest never worked). Only a real branch tip (a commit NOT on the
        # trunk's first-parent line) is genuinely merged material needing a
        # promotion decision.
        tip = (_git(repo_root, "rev-parse", branch).stdout or "").strip()
        trunk_line = (_git(repo_root, "rev-list", "--first-parent", trunk).stdout or "").splitlines()
        if tip and tip in trunk_line:
            continue
        # A tip that is a Court bookkeeping commit naming a DIFFERENT Quest is a
        # shared ledger-sync pointer: the branch was cut from / synced onto a
        # commit belonging to another Quest's lineage. `castle` is occasionally
        # rewritten (reset, not revert), so such a pointer can sit OFF the
        # first-parent line and masquerade as merged material. It carries no
        # code of this Quest's own, so it is not a rider. A real rider's tip is
        # its own last commit (a code commit or its own `court:` advance), so it
        # still flags. (Q1420, 2026-11-11.)
        if tip:
            subject = (_git(repo_root, "log", "-1", "--format=%s", tip).stdout or "").strip()
            named = re.search(r"\bQ(\d+)\b", subject)
            own = re.search(r"(\d+)", qid or "")
            if subject.lower().startswith("court: ") and named and own and named.group(1) != own.group(1):
                continue
        status = quest_status(repo_root, qid, branch=branch)
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
    """The stamped suite proof, with the lease-fence fields always present.

    ``lease_epoch`` 0 (and empty holder/role) means the proof predates fencing
    or no worker held the worktree when it ran; both are legacy-valid.
    """
    path = Path(repo_root) / ".court" / "suites" / f"{normalize_cogship_id(cogship_id)}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    payload.setdefault("lease_epoch", 0)
    payload.setdefault("lease_role", "")
    payload.setdefault("lease_holder", "")
    return payload


def audit_promotion(
    repo_root: Path,
    cogship_id: str,
    base: str = "castle",
    expected_quest_ids: Optional[Iterable[str]] = None,
    allow_ui_pending: bool = False,
    allow_stale_suite: bool = False,
    require_suite: bool = False,
    allow_pending_studio: bool = False,
) -> list[Finding]:
    """Pre-promotion audit of a convoy branch against ``base``.

    ``expected_quest_ids`` overrides the recorded pack; when neither is present
    the membership check degrades to a warning (legacy convoys predate the pack
    record) while the status and UI checks still run.

    ``allow_ui_pending`` overrides the UI-review block deliberately (the review
    is otherwise a hard refusal: an outstanding review belongs in the standing
    atelier, never in a promoted convoy). A Quest still inside an open combined
    artist studio is a hard block unless ``allow_pending_studio`` is passed: its
    reviewed artwork lives only on the studio branch, so promoting the Quest
    branch would ship pre-review code.
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
        status = quest_status(repo_root, qid, branch=source)
        if status and status not in PROMOTABLE_STATUSES:
            findings.append(Finding(
                "error",
                f"{qid} is {status} on the convoy branch — promotion requires "
                f"{'/'.join(sorted(PROMOTABLE_STATUSES))}; back {source} out of the pack.",
                quest=qid,
                branch=source,
            ))
        # A recorded UI MANDATE is an integrity finding, and --allow-ui-pending
        # must not silence it: that flag deliberately overrides the ordinary
        # outstanding-review refusal in the `elif` below, never a step the owner
        # required. Q1135 shipped on exactly that confusion.
        mandate_outstanding, mandate_detail = ui_mandate_outstanding(repo_root, qid)
        if mandate_outstanding:
            findings.append(Finding(
                "error",
                f"{qid} carries a UI MANDATE that is not satisfied — {mandate_detail}. Run the "
                f"Court Artist / UI Studio session on the rendered change and record it "
                f"(`court artist <quest>` or `court studio <ids>`), then sign off with "
                f"`court ui-signoff <quest> --by \"M'Lord\" --note \"<where said>\"`. A deferral "
                f"does not satisfy a mandate: back {source} out of the pack until the review "
                f"is evidenced, or record an explicit waiver sign-off.",
                quest=qid,
                branch=source,
                code="ui-mandate",
            ))
        elif not allow_ui_pending:
            # UI review PENDING/DEFERRED is a PACKING block, not tracked debt:
            # `court collect` refuses such a candidate so a convoy can never be
            # built that this guard would then refuse. Reaching here means the
            # pack bypassed that gate (a manual pack, a legacy convoy, or a Quest
            # whose review regressed after packing), so refuse the promotion and
            # name the atelier as the destination.
            ui_outstanding, ui_status_text = ui_review_outstanding_for(repo_root, qid)
            if ui_outstanding:
                findings.append(Finding(
                    "error",
                    f"{qid} carries an outstanding UI review "
                    f"({ui_status_text or 'no UI record'}) — {UI_REVIEW_ROUTE_HINT}, then record "
                    f"the sign-off (`court ui-signoff {qid} --by \"M'Lord\" --note \"<where said>\"`) "
                    f"and re-pack. --allow-ui-pending overrides deliberately.",
                    quest=qid,
                    branch=source,
                    code="ui-review-pending",
                ))
        if not allow_pending_studio:
            pending, detail = studio_pending(repo_root, qid)
            if pending:
                findings.append(Finding(
                    "error",
                    f"{qid} is still in an open artist studio — {detail}. Sign it off and sync the "
                    f"reviewed state back (`court studio <ids> --sync-back`, or `court studio <ids> "
                    f"--close`), then re-pack; promoting now would ship pre-review code and lose the "
                    f"studio work. (--allow-pending-studio overrides deliberately.)",
                    quest=qid,
                    branch=source,
                    code="pending-studio",
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
    if proof:
        fence_check = git_ops.check_suite_proof(cogship_id=cid, head_sha=tip, cwd=repo_root)
        if fence_check.get("fence_stale"):
            findings.append(Finding(
                "error",
                f"the green suite proof for {cid} is fenced to a superseded worker — "
                f"{fence_check.get('reason')}",
                code="stale-lease-fence",
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
    peeled = peeled_quest_ids(repo_root, trunk)
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
        if not qid or qid in seen or qid in peeled:
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
