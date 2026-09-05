"""
The Ward — Deterministic Information Presence, Gap Verification, and Compliance Auditor.

Audits Quests, Epics, and Scouts for:
1. 6-part Serf Tribute presence (Ballad, Tribute, Tally, Penance, Audience, Opinion)
   or 5-part Scout Report presence (Survey, Map, Dangers, Tribute, Plot).
2. Protocol compliance: base drift (behind > 0 vs the base branch), dirty working tree
   state, branch naming folder hierarchy, and frontmatter/worktree mappings.
3. Artifact presence: verified deliverables on disk.
4. Worktree task markdown checklist progress (tasks/*.md, .court/quests/*.md, .kilo/plans/*.md).

It also parses standalone 5-part Warden Reports (`.court/ward/reports/*.md`) produced by
a Warden's hunting-grounds patrol, and provides `audit_realm()` — the realm-wide
compliance summary used by `court ward`.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from court import git_ops, store
from court.models import Quest, validate_branch_name

SERF_REQUIRED_SECTIONS = ("ballad", "tribute", "tally", "penance", "audience", "opinion")
SCOUT_REQUIRED_SECTIONS = ("survey", "map", "dangers", "tribute", "plot")

# The 5-part Warden Report format (Survey, Stack Trace, Impact/Affected Accounts,
# Root Cause Diagnosis, Proposed Fix/Remit).
WARDEN_REPORT_SECTIONS = ("survey", "stack_trace", "impact", "root_cause", "proposed_fix")


class WardViolation(str):
    """A single blocking compliance violation surfaced by the Ward.

    Subclasses `str` so every existing consumer (f-string interpolation,
    `"text" in violation` membership checks, JSON serialization via
    `dataclasses.asdict()`/`json.dumps()`) keeps working unmodified while the
    type carries a distinct, documented name in the engine's public surface.
    """

    __slots__ = ()


class WardWarning(str):
    """A single non-blocking compliance warning surfaced by the Ward. See `WardViolation`."""

    __slots__ = ()


_PLACEHOLDER_PATTERNS = [
    re.compile(r"^\s*\[?(?:pending|todo|tbd|none yet|tribute rendered)\]?\s*$", re.I),
    re.compile(r"^\s*<[^>]+>\s*$", re.I),
    re.compile(r"^\s*<!--.*?-->\s*$", re.DOTALL),
]

CHECKLIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[\.\)])\s+\[([ xX~-])\]\s+(.+)$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
PHASE_RE = re.compile(r"\b(Phase\s+\d+|Milestone\s+\d+|Step\s+\d+|Part\s+\d+)(?::|\s*-|\s*—)?\s*(.*)", re.I)


def is_placeholder(text: str) -> bool:
    """Check if section content is empty or merely placeholder text."""
    trimmed = text.strip()
    if not trimmed:
        return True
    for pat in _PLACEHOLDER_PATTERNS:
        if pat.match(trimmed):
            return True
    return False


def parse_markdown_checklist(content: str, filepath: str = "") -> dict:
    """Parse markdown checklist items (- [ ] / - [x]) and track active phases/headings.

    Returns dict with:
    total, completed, pending, cancelled, percent, active_phase, active_heading, summary, items, file.
    """
    total = 0
    completed = 0
    pending = 0
    cancelled = 0
    items: list[dict] = []

    current_heading = ""
    current_phase = ""
    active_phase = ""
    active_heading = ""

    lines = content.splitlines()
    for line_idx, line in enumerate(lines, 1):
        line_str = line.strip()

        # Track markdown headings
        hm = HEADING_RE.match(line_str)
        if hm:
            current_heading = hm.group(2).strip()
            pm = PHASE_RE.search(current_heading)
            if pm:
                phase_label = pm.group(1).strip()
                phase_desc = pm.group(2).strip()
                current_phase = f"{phase_label}: {phase_desc}" if phase_desc else phase_label
            continue

        # Track checklist items
        cm = CHECKLIST_ITEM_RE.match(line)
        if cm:
            mark = cm.group(1)
            item_text = cm.group(2).strip()
            is_done = (mark in ("x", "X"))
            is_cancelled = (mark in ("-", "~"))

            total += 1
            if is_done:
                completed += 1
            elif is_cancelled:
                cancelled += 1
            else:
                pending += 1
                if not active_phase and current_phase:
                    active_phase = current_phase
                if not active_heading and current_heading:
                    active_heading = current_heading

            items.append({
                "text": item_text,
                "completed": is_done,
                "cancelled": is_cancelled,
                "heading": current_heading,
                "phase": current_phase,
                "line": line_idx,
                "file": str(filepath),
            })

    # If no pending items were encountered to set active_phase, infer from state
    if total > 0 and not active_phase:
        if pending == 0 and completed > 0:
            active_phase = "Complete" if not current_phase else f"{current_phase} (Complete)"
        elif current_phase:
            active_phase = current_phase

    if not active_heading and current_heading:
        active_heading = current_heading

    percent = round((completed / total) * 100.0, 1) if total > 0 else 0.0
    summary = f"{completed}/{total} ({percent:.0f}%)" if total > 0 else "0/0 (0%)"

    return {
        "total": total,
        "completed": completed,
        "pending": pending,
        "cancelled": cancelled,
        "percent": percent,
        "active_phase": active_phase,
        "active_heading": active_heading,
        "summary": summary,
        "items": items,
        "file": str(filepath),
    }


def discover_worktree_task_files(
    worktree_path: str | Path,
    app: str = "",
    quest_id: str = "",
) -> list[Path]:
    """Find all candidate task and plan markdown files in an active worktree."""
    wt = Path(worktree_path)
    if not wt.exists() or not wt.is_dir():
        return []

    candidates: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path):
        if p.is_file() and p not in seen:
            seen.add(p)
            candidates.append(p)

    # 1. Worktree quest/epic markdown file
    if quest_id:
        for d in (wt / ".court" / "quests", wt / ".court" / "epics"):
            if d.is_dir():
                exact = d / f"{quest_id}.md"
                if exact.is_file():
                    _add(exact)
                bare = quest_id.lstrip("Qq").partition("-")[0]
                if bare.isdigit():
                    prefix = f"Q{int(bare):03d}-"
                    for qp in d.glob(f"{prefix}*.md"):
                        _add(qp)

    # 2. Check git status for modified/untracked markdown files in tasks/ or plans/
    status_res = git_ops._run(["git", "status", "--porcelain=v1", "-uall"], wt)
    if status_res.get("ok"):
        for line in status_res.get("stdout", "").splitlines():
            if len(line) >= 3:
                fpath_str = line[3:].strip()
                if " -> " in fpath_str:
                    fpath_str = fpath_str.split(" -> ")[1].strip()
                if fpath_str.endswith(".md"):
                    if fpath_str.startswith("tasks/") or fpath_str.startswith(".kilo/plans/") or fpath_str.startswith(".court/"):
                        p = wt / fpath_str
                        if p.is_file():
                            _add(p)

    # 3. Project-specific task plans under tasks/apps/<app>/*.md — a common
    # convention, only activated if the worktree actually uses this layout.
    # No hardcoded app names: this is purely a directory-presence check.
    apps_root = wt / "tasks" / "apps"
    if apps_root.is_dir():
        if app:
            app_tasks_dir = apps_root / app.lower()
            if app_tasks_dir.is_dir():
                for p in sorted(app_tasks_dir.glob("*.md"), reverse=True):
                    _add(p)
        for p in sorted(apps_root.glob("*/*.md"), reverse=True):
            _add(p)

    # 4. Root task files in tasks/
    tasks_root = wt / "tasks"
    if tasks_root.is_dir():
        for fname in ("ACTIVE.md", "PLANNING.md", "BACKLOG.md"):
            p = tasks_root / fname
            if p.is_file():
                _add(p)
        for p in sorted(tasks_root.glob("*.md")):
            _add(p)

    # 5. .kilo/plans/*.md
    plans_dir = wt / ".kilo" / "plans"
    if plans_dir.is_dir():
        for p in sorted(plans_dir.glob("*.md")):
            _add(p)

    return candidates


def get_worktree_task_progress(
    worktree_path: Optional[str | Path] = None,
    quest: Optional[Quest] = None,
    app: str = "",
    quest_id: str = "",
) -> dict:
    """Discover task files in worktree and compute real-time checklist progress."""
    wt_path = Path(worktree_path) if worktree_path else None
    qid = quest_id or (quest.id if quest else "")
    app_name = app or (quest.app if quest else "")

    quest_checklist: dict = {}
    plan_checklist: dict = {}
    files_inspected: list[str] = []

    # 1. Parse Quest's own Expected Tribute (from quest obj or worktree file)
    quest_text = ""
    if quest:
        quest_text = quest.body_sections.get("Expected Tribute", "")
    elif wt_path and wt_path.is_dir() and qid:
        q_file = wt_path / ".court" / "quests" / f"{qid}.md"
        if not q_file.exists():
            q_file = wt_path / ".court" / "epics" / f"{qid}.md"
        if q_file.is_file():
            try:
                loaded_q = Quest.from_markdown(q_file.read_text(encoding="utf-8"))
                quest_text = loaded_q.body_sections.get("Expected Tribute", "")
            except Exception:
                pass

    if quest_text:
        quest_checklist = parse_markdown_checklist(
            quest_text,
            filepath=f".court/quests/{qid}.md" if qid else "quest.md",
        )

    # 2. Discover worktree task files if worktree path is available
    matched_plan = False
    if wt_path and wt_path.is_dir():
        candidate_files = discover_worktree_task_files(wt_path, app=app_name, quest_id=qid)
        short_qid = qid.split("-")[0].lower() if qid else ""
        concern_slug = quest.concern.lower().replace("-", "_") if (quest and quest.concern) else ""

        # Check git status for modified/untracked files in this worktree
        modified_files: set[str] = set()
        status_res = git_ops._run(["git", "status", "--porcelain=v1", "-uall"], wt_path)
        if status_res.get("ok"):
            for line in status_res.get("stdout", "").splitlines():
                if len(line) >= 3:
                    fpath = line[3:].strip()
                    if " -> " in fpath:
                        fpath = fpath.split(" -> ")[1].strip()
                    modified_files.add(fpath)

        for cf in candidate_files:
            try:
                rel_path = str(cf.relative_to(wt_path))
            except ValueError:
                rel_path = str(cf)
            files_inspected.append(rel_path)

            # If this candidate is the quest file itself and we already parsed it, skip
            if cf.name == f"{qid}.md" or (qid and short_qid in cf.stem.lower() and ".court" in str(cf)):
                continue

            # Check if this file is specifically matched to this quest
            is_specific_match = False
            norm_path = str(cf).lower().replace("\\", "/")
            if short_qid and short_qid in cf.stem.lower():
                is_specific_match = True
            elif concern_slug and (concern_slug in cf.stem.lower() or concern_slug.replace("_", "-") in cf.stem.lower()):
                is_specific_match = True
            elif rel_path in modified_files:
                is_specific_match = True
            elif app_name and f"tasks/apps/{app_name.lower()}" in norm_path:
                is_specific_match = True

            # If not already found a plan checklist with items, check this file
            if not plan_checklist or plan_checklist.get("total", 0) == 0 or (is_specific_match and not matched_plan):
                try:
                    content = cf.read_text(encoding="utf-8", errors="replace")
                    parsed = parse_markdown_checklist(content, filepath=rel_path)
                    if parsed.get("total", 0) > 0:
                        plan_checklist = parsed
                        if is_specific_match:
                            matched_plan = True
                except Exception:
                    pass

    # 3. Determine primary progress:
    # Prefer quest_checklist (e.g. Expected Tribute) over external worktree plans unless matched.
    if quest_checklist and quest_checklist.get("total", 0) > 0 and not matched_plan:
        primary = quest_checklist
    elif plan_checklist and plan_checklist.get("total", 0) > 0:
        primary = plan_checklist
    elif quest_checklist and quest_checklist.get("total", 0) > 0:
        primary = quest_checklist
    else:
        primary = plan_checklist or quest_checklist

    total = primary.get("total", 0) if primary else 0
    completed = primary.get("completed", 0) if primary else 0
    pending = primary.get("pending", 0) if primary else 0
    percent = primary.get("percent", 0.0) if primary else 0.0
    active_phase = primary.get("active_phase", "") if primary else ""
    active_heading = primary.get("active_heading", "") if primary else ""
    summary = primary.get("summary", "0/0 (0%)") if primary else "0/0 (0%)"
    primary_file = primary.get("file", "") if primary else ""
    items = primary.get("items", []) if primary else []

    found = total > 0

    return {
        "found": found,
        "file": primary_file,
        "total": total,
        "completed": completed,
        "pending": pending,
        "percent": percent,
        "active_phase": active_phase,
        "active_heading": active_heading,
        "summary": summary,
        "items": items,
        "quest_checklist": quest_checklist,
        "plan_checklist": plan_checklist,
        "files_inspected": files_inspected,
    }


@dataclass
class WardAudit:
    quest_id: str
    title: str
    kind: str
    status: str
    section: str
    branch: str
    worktree: str
    is_compliant: bool
    tribute_present: bool
    sections_present: list[str] = field(default_factory=list)
    missing_sections: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    git_status: dict = field(default_factory=dict)
    artifacts_checked: list[dict] = field(default_factory=list)
    task_progress: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def summary_badge(self) -> str:
        if not self.violations:
            return "COMPLIANT" if self.tribute_present else "IN PROGRESS"
        return "NON-COMPLIANT"

    def format_report(self) -> str:
        lines = [
            f"Audit Report for {self.quest_id}: {self.title}",
            f"Kind: {self.kind} | Status: [{self.status}] | Section: {self.section or '-'}",
            f"Branch: {self.branch or '-'} | Worktree: {self.worktree or '-'}",
            f"Compliance Verdict: {self.summary_badge()}",
        ]

        if self.task_progress.get("found"):
            tp = self.task_progress
            phase_str = f" | Phase: {tp['active_phase']}" if tp.get("active_phase") else ""
            file_str = f" (file: {tp['file']})" if tp.get("file") else ""
            lines.append(f"Task Progress: [{tp['summary']}]{phase_str}{file_str}")

        if self.git_status.get("exists"):
            gs = self.git_status
            behind_str = f"{gs.get('behind', 0)} commits behind base"
            ahead_str = f"{gs.get('ahead', 0)} commits ahead of base"
            dirty_str = "DIRTY" if gs.get("dirty") else "CLEAN"
            lines.append(f"Git State: [{dirty_str}] ({ahead_str}, {behind_str})")
            if gs.get("untracked"):
                lines.append(f"  - Untracked ({len(gs['untracked'])}): {', '.join(gs['untracked'][:3])}")
            if gs.get("modified"):
                lines.append(f"  - Modified ({len(gs['modified'])}): {', '.join(gs['modified'][:3])}")
            if gs.get("staged"):
                lines.append(f"  - Staged ({len(gs['staged'])}): {', '.join(gs['staged'][:3])}")
        elif self.worktree:
            lines.append(f"Git State: Worktree path not found on disk ({self.worktree})")

        # Sections
        lines.append(f"Tribute Rendered: {'Present' if self.tribute_present else 'Empty / Missing'}")
        if self.sections_present:
            lines.append(f"  - Found ({len(self.sections_present)}): {', '.join(self.sections_present)}")
        if self.missing_sections:
            lines.append(f"  - Missing ({len(self.missing_sections)}): {', '.join(self.missing_sections)}")

        # Artifacts
        if self.artifacts_checked:
            lines.append(f"Artifacts Checked ({len(self.artifacts_checked)}):")
            for art in self.artifacts_checked:
                status_icon = "✓" if art["exists"] else "✗"
                lines.append(f"  [{status_icon}] {art['path']}")

        # Violations & Warnings
        if self.violations:
            lines.append("Violations (Blocking):")
            for v in self.violations:
                lines.append(f"  - {v}")
        if self.warnings:
            lines.append("Warnings (Non-blocking):")
            for w in self.warnings:
                lines.append(f"  - {w}")

        return "\n".join(lines)


def check_tribute_sections(quest: Quest) -> tuple[bool, list[str], list[str], dict[str, str]]:
    """Inspect Quest tribute and extract required subsections.

    Returns:
    (tribute_present, sections_present, missing_sections, extracted_sections_dict)
    """
    raw_tribute = quest.body_sections.get("Tribute Rendered", "").strip()
    is_scout = quest.kind == "scout" or quest.section == "Investigation"
    required = SCOUT_REQUIRED_SECTIONS if is_scout else SERF_REQUIRED_SECTIONS

    if not raw_tribute or is_placeholder(raw_tribute):
        return False, [], list(required), {}

    extracted: dict[str, str] = {}
    present: list[str] = []
    missing: list[str] = []

    for sec in required:
        content = quest.extract_tribute_subsection(sec)
        if content and not is_placeholder(content):
            extracted[sec] = content
            present.append(sec)
        else:
            missing.append(sec)

    tribute_present = bool(present)
    return tribute_present, present, missing, extracted


def extract_expected_artifact_paths(quest: Quest) -> list[str]:
    """Parse Expected Tribute and Goal & Scope for file/directory paths."""
    text = (
        quest.body_sections.get("Expected Tribute", "")
        + "\n"
        + quest.body_sections.get("Goal & Scope", "")
    )
    # Match paths like `tasks/apps/...`, `tasks/artifacts/...`, `apps/...`, `tests/...`, `.court/...`
    pattern = re.compile(
        r"`([a-zA-Z0-9_\-\./]+(?:\.[a-zA-Z0-9]+|/))`"
    )
    paths = set()
    for m in pattern.finditer(text):
        p = m.group(1).strip()
        if "/" in p and not p.startswith("http") and not p.startswith("github"):
            paths.add(p)
    return sorted(paths)


def audit_quest(
    quest_or_id: Quest | str,
    worktree_path: Optional[str | Path] = None,
    base_branch: str = "castle",
    cwd: Optional[Path | str] = None,
    court_root: Optional[Path] = None,
) -> WardAudit:
    """Audit a single Quest for information presence, gaps, and protocol compliance."""
    if isinstance(quest_or_id, str):
        quest = store.load(quest_or_id, court_root=court_root)
    else:
        quest = quest_or_id

    violations: list[WardViolation] = []
    warnings: list[WardWarning] = []

    # 1. Branch Naming Compliance
    if quest.branch:
        is_valid_branch, branch_err = validate_branch_name(quest.branch)
        if not is_valid_branch:
            violations.append(WardViolation(branch_err))
    else:
        violations.append(WardViolation("Quest frontmatter missing 'branch' name."))

    # 2. Frontmatter Mappings
    if quest.status in ("WORKING", "REVIEW", "GATE", "READY_FOR_TEARDOWN", "DONE"):
        if not quest.serf_session_id and quest.kind != "epic":
            warnings.append(WardWarning("Missing 'serf_session_id' in frontmatter."))

    # 3. Worktree Resolution & Git Status
    wt_resolved = None
    if worktree_path:
        wt_resolved = Path(worktree_path)
    elif quest.worktree and Path(quest.worktree).is_dir():
        wt_resolved = Path(quest.worktree)
    else:
        wt_resolved = git_ops.find_worktree_for_quest(quest, cwd=cwd)

    git_stat = {}
    if wt_resolved and wt_resolved.is_dir():
        git_stat = git_ops.get_worktree_git_status(wt_resolved, base=base_branch)

        # Checked-out branch folder hierarchy check
        actual_branch = git_stat.get("branch", "")
        if actual_branch:
            is_valid_actual, actual_branch_err = validate_branch_name(actual_branch)
            if not is_valid_actual:
                target_branch_hint = quest.branch or "canonical/slash-branch"
                violations.append(
                    f"Checked-out branch in worktree is flat '{actual_branch}'. "
                    f"Must use slash hierarchy (run: git branch -m {target_branch_hint})."
                )
            elif quest.branch and actual_branch != quest.branch:
                violations.append(
                    f"Worktree branch mismatch: checked-out branch '{actual_branch}' does not match quest frontmatter branch '{quest.branch}'."
                )

        # Base drift check (behind > 0 vs base_branch)
        behind = git_stat.get("behind")
        if behind is not None and behind > 0:
            if quest.status == "WORKING":
                # Tolerated during WORKING; a deferred rebase is required before REVIEW.
                warnings.append(WardWarning(
                    f"Worktree is {behind} commit(s) behind {base_branch} (tolerated in WORKING; "
                    f"deferred rebase `git merge {base_branch}` required before REVIEW)."
                ))
            elif quest.status in ("REVIEW", "GATE", "READY_FOR_TEARDOWN"):
                # The deferred-rebase requirement is a one-time *entry* gate, enforced
                # independently and freshly at the moment of the WORKING -> REVIEW
                # transition itself (see `cmd_levy`'s auto-advance re-check of
                # `behind == 0` right before calling `set_status`). Re-litigating it
                # here as an ongoing violation on every later audit would make the
                # queue unable to converge: the base branch keeps moving as *other*
                # Quests advance/get promoted while this one just waits its turn,
                # so it would immediately get flagged non-compliant through no
                # fault of its own. Bounded drift accumulated *after* a clean entry
                # is tolerated at every downstream stage and absorbed by the next
                # `court rebase`/`court levy` sweep, or by the Gatekeeper during
                # Cog Ship packing.
                warnings.append(WardWarning(
                    f"Bounded residual drift: worktree is {behind} commit(s) behind {base_branch} at {quest.status} "
                    f"(tolerated after a clean entry; absorbed by the next `court rebase`/`court levy` sweep or by the Gatekeeper during Cog Ship packing)."
                ))
            else:
                violations.append(WardViolation(
                    f"Base drift: worktree is {behind} commit(s) behind {base_branch}."
                ))

        # Dirty working tree check
        if git_stat.get("dirty"):
            dirty_count = (
                len(git_stat.get("untracked", []))
                + len(git_stat.get("modified", []))
                + len(git_stat.get("staged", []))
                + len(git_stat.get("deleted", []))
            )
            dirty_sample = (
                git_stat.get("untracked", [])
                + git_stat.get("modified", [])
                + git_stat.get("staged", [])
                + git_stat.get("deleted", [])
            )[:3]
            sample_str = ", ".join(dirty_sample)
            violations.append(WardViolation(
                f"Dirty working tree: {dirty_count} uncommitted file(s) ({sample_str})."
            ))

        # Ahead commits check (if review/gate or tribute present)
        ahead = git_stat.get("ahead")
        if ahead == 0 and quest.status in ("REVIEW", "GATE", "READY_FOR_TEARDOWN"):
            violations.append(WardViolation(
                f"Zero commits delivered on branch (0 commits ahead of {base_branch})."
            ))
    else:
        # No worktree directory resolved on disk. DISPATCHED/WORKING quests are
        # supposed to have a live worktree (Serf spawned into it) — that's a
        # blocking violation. REVIEW/GATE quests may have already had their
        # worktree pruned mid-handoff, so that's only a warning.
        if quest.status in ("DISPATCHED", "WORKING", "REVIEW", "GATE"):
            warnings.append(WardWarning(
                f"No active git worktree directory resolved for {quest.id} (branch: {quest.branch or '-'})."
            ))
            if quest.status in ("WORKING", "DISPATCHED"):
                violations.append(WardViolation(
                    f"No active git worktree directory on disk (worktree missing or pruned)."
                ))

    # 4. Task Markdown Checklist Progress
    task_prog = get_worktree_task_progress(
        worktree_path=wt_resolved,
        quest=quest,
        app=quest.app,
        quest_id=quest.id,
    )

    # 5. Tribute Rendered & Section Completeness
    tribute_present, present_secs, missing_secs, extracted = check_tribute_sections(quest)

    if quest.status in ("REVIEW", "GATE", "READY_FOR_TEARDOWN", "DONE"):
        if not tribute_present:
            violations.append(WardViolation("Missing '# Tribute Rendered' body section."))
        elif missing_secs:
            violations.append(WardViolation(
                f"Incomplete tribute: missing required subsection(s): {', '.join(missing_secs)}."
            ))
    elif quest.status in ("WORKING", "DISPATCHED"):
        if tribute_present and missing_secs:
            warnings.append(WardWarning(
                f"Partial tribute rendered: missing {', '.join(missing_secs)}."
            ))

    # 6. Check Test Proof
    if tribute_present:
        tribute_body = extracted.get("tribute", "") + "\n" + extracted.get("tally", "")
        test_indicators = ("pytest", "python -m unittest", "exit_code", "passed", "test_", "run_test_command")
        has_test_mention = any(ind in tribute_body.lower() for ind in test_indicators)
        if not has_test_mention and quest.kind != "epic" and quest.section != "Investigation":
            warnings.append(WardWarning("No explicit test command or exit code record detected in Tribute/Tally."))

    # 7. Artifact Existence Verification
    artifacts_checked: list[dict] = []
    expected_paths = extract_expected_artifact_paths(quest)
    search_root = wt_resolved if (wt_resolved and wt_resolved.is_dir()) else (Path(cwd) if cwd else Path.cwd())

    for p_str in expected_paths:
        candidate = search_root / p_str
        exists = candidate.exists()
        artifacts_checked.append({"path": p_str, "exists": exists})
        # If an artifact in tasks/ or tests/ was expected and quest is in REVIEW/GATE, check existence
        if not exists and quest.status in ("REVIEW", "GATE"):
            if p_str.startswith("tasks/") or p_str.startswith("tests/"):
                warnings.append(WardWarning(f"Expected artifact path not found on disk: {p_str}"))

    is_compliant = len(violations) == 0

    return WardAudit(
        quest_id=quest.id,
        title=quest.title,
        kind=quest.kind,
        status=quest.status,
        section=quest.section,
        branch=quest.branch,
        worktree=str(wt_resolved) if wt_resolved else quest.worktree,
        is_compliant=is_compliant,
        tribute_present=tribute_present,
        sections_present=present_secs,
        missing_sections=missing_secs,
        violations=violations,
        warnings=warnings,
        git_status=git_stat,
        artifacts_checked=artifacts_checked,
        task_progress=task_prog,
    )


def audit_all_quests(
    status: Optional[str] = None,
    app: Optional[str] = None,
    epic: Optional[str] = None,
    include_archive: bool = False,
    base_branch: str = "castle",
    cwd: Optional[Path | str] = None,
    court_root: Optional[Path] = None,
) -> list[WardAudit]:
    """Audit all Quests matching filters."""
    quests = store.list_all(include_archive=include_archive, court_root=court_root)

    if app:
        quests = [q for q in quests if q.app.lower() == app.lower()]
    if epic:
        epic_norm = epic.lower().lstrip("q").partition("-")[0]
        quests = [
            q for q in quests
            if q.parent_epic.lower().lstrip("q").partition("-")[0] == epic_norm
            or q.id.lower().lstrip("q").partition("-")[0] == epic_norm
        ]
    if status:
        status_set = {s.strip().upper() for s in status.split(",")}
        quests = [q for q in quests if q.status in status_set]

    results = []
    for q in quests:
        res = audit_quest(q, base_branch=base_branch, cwd=cwd, court_root=court_root)
        results.append(res)

    return results


def sync_tribute_from_worktree(
    quest: Quest,
    worktree_path: Optional[Path | str] = None,
    cwd: Optional[Path | str] = None,
    court_root: Optional[Path] = None,
) -> tuple[bool, str]:
    """Auto-sync `# Tribute Rendered`, `# Expected Tribute`, and frontmatter from a worktree into the master quest file."""
    wt = None
    if worktree_path:
        wt = Path(worktree_path)
    elif quest.worktree and Path(quest.worktree).is_dir():
        wt = Path(quest.worktree)
    else:
        wt = git_ops.find_worktree_for_quest(quest, cwd=cwd)

    if not wt or not wt.is_dir():
        return False, f"No worktree found for {quest.id}"

    # Search for quest markdown in the worktree
    candidate_paths = [
        wt / ".court" / "quests" / f"{quest.id}.md",
        wt / ".court" / "epics" / f"{quest.id}.md",
    ]
    # Also check glob for Q0NN-*.md
    short_num = quest.id.split("-")[0].lstrip("Qq")
    prefix = f"Q{short_num}-"
    for d in (wt / ".court" / "quests", wt / ".court" / "epics"):
        if d.is_dir():
            candidate_paths.extend(list(d.glob(f"{prefix}*.md")))

    src_file = None
    for cp in candidate_paths:
        if cp.exists() and cp.is_file():
            src_file = cp
            break

    if not src_file:
        return False, f"No quest markdown found inside worktree {wt}"

    try:
        wt_quest = Quest.from_markdown(src_file.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"Failed to parse quest file in worktree: {e}"

    modified = False
    notes = []

    # Check tribute rendered
    wt_tribute = wt_quest.body_sections.get("Tribute Rendered", "").strip()
    curr_tribute = quest.body_sections.get("Tribute Rendered", "").strip()

    if wt_tribute and (wt_tribute != curr_tribute):
        quest.body_sections["Tribute Rendered"] = wt_tribute
        modified = True
        notes.append("Synced Tribute Rendered")

    # Check expected tribute
    wt_expected = wt_quest.body_sections.get("Expected Tribute", "").strip()
    curr_expected = quest.body_sections.get("Expected Tribute", "").strip()

    if wt_expected and (wt_expected != curr_expected):
        quest.body_sections["Expected Tribute"] = wt_expected
        modified = True
        notes.append("Synced Expected Tribute")

    # Sync frontmatter if worktree has newer or missing values
    if wt_quest.serf_session_id and not quest.serf_session_id:
        quest.serf_session_id = wt_quest.serf_session_id
        modified = True
        notes.append("Synced serf_session_id")
    if wt_quest.serf_model and not quest.serf_model:
        quest.serf_model = wt_quest.serf_model
        modified = True
        notes.append("Synced serf_model")
    if not quest.worktree:
        quest.worktree = str(wt)
        modified = True
        notes.append("Mapped worktree path")

    if modified:
        store.save(quest, court_root=court_root)
        return True, "; ".join(notes)

    return False, "Already up to date"


def audit_realm(
    base_branch: str = "castle",
    include_archive: bool = False,
    court_root: Optional[Path] = None,
) -> dict:
    """Realm-wide compliance health summary used by `court ward`.

    Audits every in-flight Quest/Epic (WORKING, DISPATCHED, REVIEW, GATE) for
    base drift, dirty worktrees, and tribute completeness, and returns an
    aggregate scorecard alongside the individual `WardAudit` records.
    """
    results = audit_all_quests(
        status="WORKING,DISPATCHED,REVIEW,GATE",
        include_archive=include_archive,
        base_branch=base_branch,
        court_root=court_root,
    )
    non_compliant = [r for r in results if not r.is_compliant]
    dirty = [r for r in results if r.git_status.get("dirty")]
    behind = [r for r in results if (r.git_status.get("behind") or 0) > 0]

    return {
        "total_active": len(results),
        "compliant_count": len(results) - len(non_compliant),
        "non_compliant_count": len(non_compliant),
        "dirty_count": len(dirty),
        "behind_count": len(behind),
        "audits": results,
    }


# ---------------------------------------------------------------------------
# Warden Report parsing — standalone 5-part investigation briefs produced by
# a Warden's hunting-grounds patrol (Survey, Stack Trace, Impact / Affected
# Accounts, Root Cause Diagnosis, Proposed Fix / Remit). These live outside
# any Quest's Tribute Rendered section, typically at `.court/ward/reports/*.md`.
# ---------------------------------------------------------------------------

_WARDEN_SECTION_ALIASES = {
    "survey": "survey",
    "the survey": "survey",
    "stack trace": "stack_trace",
    "the stack trace": "stack_trace",
    "stacktrace": "stack_trace",
    "impact": "impact",
    "impact affected accounts": "impact",
    "affected accounts": "impact",
    "impact and affected accounts": "impact",
    "root cause diagnosis": "root_cause",
    "root cause": "root_cause",
    "the root cause diagnosis": "root_cause",
    "proposed fix remit": "proposed_fix",
    "proposed fix": "proposed_fix",
    "remit": "proposed_fix",
    "the proposed fix remit": "proposed_fix",
}


def parse_warden_report(text: str, filepath: str = "") -> dict:
    """Parse a standalone 5-part Warden Report markdown document.

    Unlike `Quest.extract_tribute_subsection` (which reads a Quest's embedded
    `Tribute Rendered` body), this operates on a standalone Warden Report
    string/file and extracts the canonical sections: `survey`, `stack_trace`,
    `impact`, `root_cause`, `proposed_fix`.

    Returns a dict with `sections`, `present`, `missing`, `complete`, `file`.
    """
    sections: dict[str, str] = {}
    current_key: Optional[str] = None
    buf: list[str] = []

    for line in text.splitlines():
        m = HEADING_RE.match(line.strip())
        matched_key = None
        if m:
            raw_title = m.group(2).strip()
            raw_title = re.sub(r"^\(?\d+[\.\)]\s*", "", raw_title)
            cleaned = re.sub(r"[^\w\s/_-]", "", raw_title).lower().strip()
            cleaned = cleaned.replace("/", " ").replace("_", " ").replace("-", " ")
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            matched_key = _WARDEN_SECTION_ALIASES.get(cleaned)
            if not matched_key:
                for k, v in _WARDEN_SECTION_ALIASES.items():
                    if cleaned == k or cleaned.startswith(k + " ") or cleaned.endswith(" " + k):
                        matched_key = v
                        break

        if matched_key:
            if current_key is not None:
                sections[current_key] = "\n".join(buf).strip()
            current_key = matched_key
            buf = []
            continue
        buf.append(line)

    if current_key is not None:
        sections[current_key] = "\n".join(buf).strip()

    present = [s for s in WARDEN_REPORT_SECTIONS if sections.get(s) and not is_placeholder(sections[s])]
    missing = [s for s in WARDEN_REPORT_SECTIONS if s not in present]

    return {
        "sections": sections,
        "present": present,
        "missing": missing,
        "complete": len(missing) == 0,
        "file": str(filepath),
    }


def parse_warden_report_file(path: Path | str) -> dict:
    """Read and parse a Warden Report markdown file from disk. See `parse_warden_report`."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    return parse_warden_report(text, filepath=str(p))
