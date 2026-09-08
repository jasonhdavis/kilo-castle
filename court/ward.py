"""
The Ward — Deterministic Information Presence, Gap Verification, and Compliance Auditor.

This is the Court's internal compliance subsystem, patrolled by Warden agents
and surfaced to the Steward via `court ward`.

Audits Quests, Epics, and Scouts for:
1. 6-part Serf Tribute presence (Ballad, Tribute, Tally, Penance, Audience, Opinion)
   or 5-part Scout Report presence (Survey, Map, Dangers, Tribute, Plot).
2. Protocol compliance: Base drift (behind > 0 vs base branch), dirty working tree state,
   branch naming folder hierarchy, and frontmatter/worktree mappings.
3. Artifact presence: verified deliverables on disk.
4. Worktree task markdown checklist progress (tasks/*.md, .court/quests/*.md, .kilo/plans/*.md).

It also parses standalone 5-part Warden Reports (`.court/ward/reports/*.md`) produced by
a Warden's hunting-grounds patrol, and provides `audit_realm()` — the realm-wide
compliance summary used by `court ward`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import git_ops, store
from .models import STATUSES, Quest, validate_branch_name

SERF_REQUIRED_SECTIONS = ("ballad", "tribute", "tally", "penance", "audience", "opinion")
SCOUT_REQUIRED_SECTIONS = ("survey", "map", "dangers", "tribute", "plot")

WARDEN_REPORT_SECTIONS = ("survey", "stack_trace", "impact", "root_cause", "proposed_fix")


@dataclass(frozen=True)
class WardViolation(str):
    message: str = ""
    blocking: bool = True

    def __new__(cls, message: str = "", blocking: bool = True):
        obj = str.__new__(cls, message)
        return obj

    def __str__(self) -> str:
        return self.message or str.__str__(self)

    def lower(self) -> str:
        return (self.message or str.__str__(self)).lower()


@dataclass(frozen=True)
class WardWarning(str):
    message: str = ""
    blocking: bool = False

    def __new__(cls, message: str = "", blocking: bool = False):
        obj = str.__new__(cls, message)
        return obj

    def __str__(self) -> str:
        return self.message or str.__str__(self)

    def lower(self) -> str:
        return (self.message or str.__str__(self)).lower()

_PLACEHOLDER_PATTERNS = [
    re.compile(r"^\s*\[?(?:pending|todo|tbd|none yet|tribute rendered)\]?\s*$", re.I),
    re.compile(r"^\s*<[^>]+>\s*$", re.I),
    re.compile(r"^\s*<!--.*?-->\s*$", re.DOTALL),
]


def _is_placeholder_or_empty(text: str) -> bool:
    trimmed = text.strip()
    if not trimmed:
        return True
    for pat in _PLACEHOLDER_PATTERNS:
        if pat.match(trimmed):
            return True
    return False


def check_tribute_sections(quest: Quest) -> tuple[bool, list[str], list[str], dict[str, str]]:
    """
    Check for presence of required subsections in Tribute Rendered.
    Returns:
        (tribute_present, present_sections, missing_sections, extracted_dict)
    """
    raw_tribute = quest.body_sections.get("Tribute Rendered", "").strip()
    if not raw_tribute or _is_placeholder_or_empty(raw_tribute):
        return False, [], list(SERF_REQUIRED_SECTIONS), {}

    is_scout = (quest.kind == "scout") or (quest.section == "Investigation")
    req_secs = SCOUT_REQUIRED_SECTIONS if is_scout else SERF_REQUIRED_SECTIONS

    extracted = {}
    present = []
    missing = []

    for sec in req_secs:
        val = quest.extract_tribute_subsection(sec)
        if val and not _is_placeholder_or_empty(val):
            extracted[sec] = val
            present.append(sec)
        else:
            missing.append(sec)

    return True, present, missing, extracted


def is_placeholder(text: str) -> bool:
    return _is_placeholder_or_empty(text)


def parse_markdown_checklist(markdown_content: str, filepath: Optional[str] = None) -> dict[str, Any]:
    """Parse markdown text for task checklists (- [ ] / - [x] / - [~])."""
    checked = 0
    unchecked = 0
    cancelled = 0
    active_phase = ""
    current_phase = ""

    for line in markdown_content.splitlines():
        line_s = line.strip()
        m_phase = re.match(r"^#{1,3}\s+(.+)$", line_s)
        if m_phase:
            header_text = m_phase.group(1).strip()
            if "phase" in header_text.lower():
                current_phase = header_text
                if not active_phase:
                    active_phase = current_phase

        if re.match(r"^[-*]\s+\[[xX]\]", line_s):
            checked += 1
        elif re.match(r"^[-*]\s+\[\s\]", line_s):
            unchecked += 1
            if current_phase and not active_phase:
                active_phase = current_phase
        elif re.match(r"^[-*]\s+\[[~-]\]", line_s):
            cancelled += 1

    total = checked + unchecked + cancelled
    pct = int((checked / total) * 100) if total > 0 else 0
    return {
        "filepath": filepath,
        "file": filepath,
        "checked": checked,
        "completed": checked,
        "unchecked": unchecked,
        "pending": unchecked,
        "cancelled": cancelled,
        "total": total,
        "percent": pct,
        "active_phase": active_phase or "Phase 1",
    }


def discover_worktree_task_files(worktree_path: str | Path, app: Optional[str] = None) -> list[Path]:
    """Discover task checklist markdown files within a worktree."""
    wt = Path(worktree_path)
    if not wt.is_dir():
        return []
    candidates: list[Path] = []
    search_dirs = [wt / "tasks", wt / ".kilo" / "plans"]
    if app:
        search_dirs.append(wt / "tasks" / "apps" / app)
    for d in search_dirs:
        if d.is_dir():
            for p in sorted(d.glob("**/*.md")):
                if p.is_file() and p not in candidates:
                    candidates.append(p)
    return candidates


def parse_worktree_task_progress(
    worktree_path: str | Path = "",
    quest: Optional[Quest] = None,
    task_file_rel: Optional[str] = None,
    app: Optional[str] = None,
    quest_id: Optional[str] = None,
    **kwargs,
) -> dict:
    """Discover and parse task checklist progress within a worktree."""
    if not worktree_path and quest:
        found_wt = git_ops.find_worktree_for_quest(quest)
        if found_wt:
            worktree_path = found_wt

    wt = Path(worktree_path) if worktree_path else None
    if not wt or not wt.exists() or not wt.is_dir():
        if quest:
            expected_tribute = quest.body_sections.get("Expected Tribute", "")
            if expected_tribute:
                parsed_et = parse_markdown_checklist(expected_tribute)
                if parsed_et["total"] > 0:
                    return {
                        "task_file": f".court/quests/{quest.id}.md (Expected Tribute)",
                        "checked": parsed_et["checked"],
                        "completed": parsed_et["completed"],
                        "unchecked": parsed_et["unchecked"],
                        "pending": parsed_et["pending"],
                        "total": parsed_et["total"],
                        "percent": parsed_et["percent"],
                        "source": "expected_tribute",
                    }
        return {
            "task_file": None,
            "checked": 0,
            "completed": 0,
            "unchecked": 0,
            "pending": 0,
            "total": 0,
            "percent": 0,
            "source": "none",
        }

    quest_checklist = None
    if quest:
        expected_tribute = quest.body_sections.get("Expected Tribute", "")
        if expected_tribute:
            parsed_et = parse_markdown_checklist(expected_tribute)
            if parsed_et["total"] > 0:
                quest_checklist = parsed_et

    matched_plan = None
    plan_checklist = None

    if task_file_rel:
        candidate = wt / task_file_rel
        if candidate.is_file():
            try:
                content = candidate.read_text(encoding="utf-8")
                parsed = parse_markdown_checklist(content)
                if parsed["total"] > 0:
                    matched_plan = task_file_rel
                    plan_checklist = parsed
            except Exception:
                pass

    if not matched_plan:
        short_qid = quest.id.split("-")[0].lower() if quest else None
        concern_slug = quest.concern.lower() if quest else None
        app_name = quest.app.lower() if quest else None

        candidates: list[Path] = []
        seen: set[Path] = set()

        def _add(p: Path):
            if p not in seen and p.is_file():
                seen.add(p)
                candidates.append(p)

        # 1. Look in worktree root .court/quests/
        if short_qid:
            court_quests = wt / ".court" / "quests"
            if court_quests.is_dir():
                for p in court_quests.glob(f"{short_qid.upper()}*.md"):
                    _add(p)

        # 2. Check git status for recently modified markdown files
        status_res = git_ops._run(["git", "status", "--porcelain=v1", "-uall"], wt)
        modified_files: list[str] = []
        if status_res.get("ok"):
            for line in status_res.get("stdout", "").splitlines():
                if len(line) >= 3:
                    f_rel = line[3:].strip()
                    if f_rel.endswith(".md"):
                        modified_files.append(f_rel)
                        p = wt / f_rel
                        if p.is_file():
                            _add(p)

        # 3. App-specific task plans in tasks/apps/<app>/
        if app_name:
            app_tasks_dir = wt / "tasks" / "apps" / app_name
            if app_tasks_dir.is_dir():
                for p in sorted(app_tasks_dir.glob("*.md"), reverse=True):
                    _add(p)

        # 4. Other app task plans in tasks/apps/*/*.md
        all_apps_dir = wt / "tasks" / "apps"
        if all_apps_dir.is_dir():
            for p in sorted(all_apps_dir.glob("*/*.md"), reverse=True):
                _add(p)

        # 5. Root task files in tasks/
        tasks_root = wt / "tasks"
        if tasks_root.is_dir():
            for fname in ("ACTIVE.md", "PLANNING.md", "BACKLOG.md"):
                p = tasks_root / fname
                if p.is_file():
                    _add(p)
            for p in sorted(tasks_root.glob("*.md")):
                _add(p)

        # 6. .kilo/plans/*.md
        plans_dir = wt / ".kilo" / "plans"
        if plans_dir.is_dir():
            for p in sorted(plans_dir.glob("*.md")):
                _add(p)

        for cf in candidates:
            try:
                rel_path = str(cf.relative_to(wt))
            except ValueError:
                rel_path = str(cf)

            if rel_path.startswith(".court/quests/") or rel_path.startswith(".court/archive/"):
                continue

            try:
                content = cf.read_text(encoding="utf-8")
            except Exception:
                continue

            parsed = parse_markdown_checklist(content)
            if parsed["total"] == 0:
                continue

            is_specific_match = False
            if short_qid and short_qid in cf.stem.lower():
                is_specific_match = True
            elif concern_slug and (concern_slug in cf.stem.lower() or concern_slug.replace("_", "-") in cf.stem.lower()):
                is_specific_match = True
            elif rel_path in modified_files:
                is_specific_match = True

            if not plan_checklist or plan_checklist.get("total", 0) == 0 or (is_specific_match and not matched_plan):
                matched_plan = rel_path
                plan_checklist = parsed
                if is_specific_match:
                    break

    if plan_checklist and plan_checklist["total"] > 0:
        return {
            "task_file": matched_plan,
            "checked": plan_checklist["checked"],
            "unchecked": plan_checklist["unchecked"],
            "total": plan_checklist["total"],
            "percent": plan_checklist["percent"],
            "source": "task_file",
        }

    if quest_checklist and quest_checklist["total"] > 0:
        return {
            "task_file": f".court/quests/{quest.id}.md (Expected Tribute)",
            "checked": quest_checklist["checked"],
            "unchecked": quest_checklist["unchecked"],
            "total": quest_checklist["total"],
            "percent": quest_checklist["percent"],
            "source": "expected_tribute",
        }

    return {
        "task_file": matched_plan,
        "checked": 0,
        "unchecked": 0,
        "total": 0,
        "percent": 0,
        "source": "none",
    }


get_worktree_task_progress = parse_worktree_task_progress


@dataclass
class WardAudit:
    quest: Quest
    worktree_path: Optional[str]
    git_status: dict
    task_progress: dict
    tribute_present: bool
    present_sections: list[str]
    missing_sections: list[str]
    extracted_sections: dict[str, str]
    test_proof_found: bool
    artifacts_checked: list[dict]
    violations: list[WardViolation] = field(default_factory=list)
    warnings: list[WardWarning] = field(default_factory=list)

    @property
    def sections_present(self) -> list[str]:
        return self.present_sections

    @property
    def quest_id(self) -> str:
        return self.quest.id

    @property
    def is_compliant(self) -> bool:
        return len(self.violations) == 0

    def summary_badge(self) -> str:
        if not self.violations:
            return "COMPLIANT" if self.tribute_present else "IN PROGRESS"
        return "NON-COMPLIANT"

    def to_dict(self) -> dict:
        return {
            "quest_id": self.quest.id,
            "status": self.quest.status,
            "is_compliant": self.is_compliant,
            "summary": self.summary_badge(),
            "violations": [str(v) for v in self.violations],
            "warnings": [str(w) for w in self.warnings],
            "git_status": self.git_status,
            "task_progress": self.task_progress,
            "tribute_present": self.tribute_present,
            "present_sections": self.present_sections,
            "missing_sections": self.missing_sections,
        }

    def format_report(self) -> str:
        lines = [
            f"=== Ward Compliance Audit: {self.quest.id} ({self.quest.title}) ===",
            f"Status: {self.quest.status} | Branch: {self.quest.branch or '-'} | Worktree: {self.worktree_path or '-'}",
            f"Result: {self.summary_badge()}",
            "",
            "1. Information & Tribute Presence:",
            f"   - Tribute Rendered Present: {'YES' if self.tribute_present else 'NO'}",
            f"   - Present Subsections: {', '.join(self.present_sections) if self.present_sections else 'None'}",
            f"   - Missing Subsections: {', '.join(self.missing_sections) if self.missing_sections else 'None'}",
            f"   - Test Execution Proof: {'FOUND' if self.test_proof_found else 'MISSING / UNVERIFIED'}",
            "",
            "2. Task Progress (Checklist):",
            f"   - Source File: {self.task_progress.get('task_file') or 'None detected'}",
            f"   - Progress: {self.task_progress.get('checked', 0)}/{self.task_progress.get('total', 0)} ({self.task_progress.get('percent', 0)}%)",
            "",
            "3. Protocol Compliance & Git Worktree:",
            f"   - Worktree State: {'DIRTY' if self.git_status.get('dirty') else 'CLEAN'}",
            f"   - Base Drift: {self.git_status.get('behind', 0)} commit(s) behind base branch",
            f"   - Ahead Commits: {self.git_status.get('ahead', 0)} commit(s) ahead",
        ]

        if self.artifacts_checked:
            lines.append("")
            lines.append("4. Artifact Presence Check:")
            for art in self.artifacts_checked:
                status_str = "FOUND" if art["exists"] else "MISSING"
                lines.append(f"   - [{status_str}] {art['path']}")

        lines.append("")
        if self.violations:
            lines.append("Violations (Blocking):")
            for v in self.violations:
                lines.append(f"  - {v}")
        if self.warnings:
            lines.append("Warnings (Non-blocking):")
            for w in self.warnings:
                lines.append(f"  - {w}")

        return "\n".join(lines)


def extract_expected_artifact_paths(quest: Quest) -> list[str]:
    """Parse Expected Tribute and The Kingdom Requires for file/directory paths."""
    text = (
        quest.body_sections.get("Expected Tribute", "")
        + "\n"
        + quest.body_sections.get("The Kingdom Requires", "")
    )
    pattern = re.compile(
        r"(?:(?:tasks|apps|tests|\.court|\.kilo|scripts|src)/[A-Za-z0-9_./-]+|\b[A-Za-z0-9_-]+\.(?:py|md|html|json|sql|sh)\b)"
    )
    paths = []
    for line in text.splitlines():
        for match in pattern.findall(line):
            cleaned = match.strip("`'\",:()")
            if cleaned and cleaned not in paths and not cleaned.endswith("."):
                paths.append(cleaned)
    return paths


def audit_quest(
    quest_or_id: str | Quest,
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

    # 1. Branch Naming Validation
    if quest.branch:
        is_valid, err_msg = validate_branch_name(quest.branch)
        if not is_valid:
            violations.append(WardViolation(err_msg))
    else:
        violations.append(WardViolation("Quest frontmatter missing 'branch' name."))

    # 2. Frontmatter Mappings
    if quest.status in ("QUESTING", "WORKING", "TRIBUTE_READY", "GATE", "READY_TO_RAZE", "LANDED", "LAUNCHED", "DONE"):
        if not quest.serf_session_id and quest.kind != "epic":
            warnings.append(WardWarning("Missing 'serf_session_id' in frontmatter."))

    # 3. Worktree Path & Status
    wt_p = None
    if worktree_path:
        wt_p = Path(worktree_path)
    else:
        found_p = git_ops.find_worktree_for_quest(quest, cwd=cwd)
        if found_p:
            wt_p = found_p

    git_stat = {}
    if wt_p and wt_p.exists() and wt_p.is_dir():
        git_stat = git_ops.get_worktree_git_status(wt_p, base=base_branch)

        # Dirty worktree check
        if git_stat.get("dirty"):
            uncommitted = (
                git_stat.get("untracked", [])
                + git_stat.get("modified", [])
                + git_stat.get("staged", [])
                + git_stat.get("deleted", [])
            )
            violations.append(WardViolation(
                f"Worktree has {len(uncommitted)} uncommitted file(s) ({', '.join(uncommitted[:3])}{'...' if len(uncommitted)>3 else ''})."
            ))

        # Check worktree branch matches frontmatter and hierarchy
        actual_branch = git_stat.get("branch")
        if actual_branch:
            clean_actual = actual_branch.replace("refs/heads/", "")
            is_valid_actual, actual_branch_err = validate_branch_name(clean_actual)
            if not is_valid_actual:
                target_branch_hint = quest.branch or "canonical/slash-branch"
                violations.append(WardViolation(
                    f"Checked-out branch in worktree is flat '{actual_branch}'. "
                    f"Must use slash hierarchy (run: git branch -m {target_branch_hint})."
                ))
            if quest.branch:
                clean_quest_b = quest.branch.replace("refs/heads/", "")
                if clean_actual != clean_quest_b:
                    violations.append(WardViolation(
                        f"Worktree branch mismatch: checked-out branch '{actual_branch}' does not match quest frontmatter branch '{quest.branch}'."
                    ))

        # Base drift check
        behind = git_stat.get("behind")
        if behind is not None and behind > 0:
            if quest.status in ("QUESTING", "WORKING"):
                warnings.append(WardWarning(
                    f"Worktree is {behind} commit(s) behind {base_branch} (tolerated in QUESTING under drift immunity; deferred rebase merge castle required before TRIBUTE_READY)."
                ))
            elif quest.status in ("TRIBUTE_READY", "GATE", "READY_TO_RAZE"):
                warnings.append(WardWarning(
                    f"Bounded residual drift: worktree is {behind} commit(s) behind {base_branch} at {quest.status} "
                    f"(tolerated after a clean entry; absorbed by the next `court rebase`/`court levy` sweep or by the Gatekeeper during Cog Ship packing)."
                ))
            else:
                violations.append(WardViolation(
                    f"Worktree is {behind} commit(s) behind {base_branch} at {quest.status}."
                ))

        # Ahead commits check
        ahead = git_stat.get("ahead")
        if ahead == 0 and quest.status in ("TRIBUTE_READY", "GATE", "READY_TO_RAZE"):
            violations.append(WardViolation(
                f"Zero commits delivered on branch (0 commits ahead of {base_branch})."
            ))

        # Charter Integrity & Anti-Tampering Check
        charter_check = git_ops.check_charter_integrity(quest, wt_p, base=base_branch)
        if charter_check.get("tampered"):
            for v in charter_check.get("violations", []):
                violations.append(WardViolation(v))
    else:
        if quest.status in ("CHARTERED", "DISPATCHED", "QUESTING", "WORKING", "TRIBUTE_READY", "GATE"):
            warnings.append(WardWarning(
                f"No active git worktree directory resolved for {quest.id} (branch: {quest.branch or '-'})."
            ))
            if quest.status in ("QUESTING", "WORKING", "CHARTERED", "DISPATCHED"):
                violations.append(WardViolation(
                    f"No active git worktree directory on disk (worktree missing or pruned)."
                ))

    # 4. Task Progress
    task_prog = {}
    if wt_p:
        task_prog = parse_worktree_task_progress(wt_p, quest=quest, task_file_rel=quest.task_file)
    else:
        task_prog = {
            "task_file": None,
            "checked": 0,
            "unchecked": 0,
            "total": 0,
            "percent": 0,
            "source": "none",
        }

    # 5. Tribute Rendered & Section Completeness
    tribute_present, present_secs, missing_secs, extracted = check_tribute_sections(quest)

    if quest.status in ("TRIBUTE_READY", "GATE", "READY_TO_RAZE", "LANDED", "LAUNCHED", "DONE"):
        if not tribute_present:
            violations.append(WardViolation("Missing '# Tribute Rendered' body section."))
        elif missing_secs:
            violations.append(WardViolation(
                f"Incomplete tribute: missing required subsection(s): {', '.join(missing_secs)}."
            ))
    elif quest.status in ("QUESTING", "WORKING", "CHARTERED", "DISPATCHED"):
        if tribute_present and missing_secs:
            warnings.append(WardWarning(
                f"Partial tribute rendered: missing {', '.join(missing_secs)}."
            ))

    # 5b. Pending Serf Audience Check
    if quest.status in ("TRIBUTE_READY", "GATE") and quest.has_pending_audience():
        warnings.append(WardWarning(
            "Pending Serf Audience: Serf documented an open decision in Tribute requiring royal judgment; resolve via /audience or record decision in Audience Log before packing."
        ))

    # 6. Check Test Proof
    if tribute_present:
        tribute_body = extracted.get("tribute", "") + "\n" + extracted.get("tally", "")
        test_proof = (
            "passed" in tribute_body.lower()
            or "ok" in tribute_body.lower()
            or "pytest" in tribute_body.lower()
            or "manage.py test" in tribute_body.lower()
            or "test_" in tribute_body.lower()
            or "exit code 0" in tribute_body.lower()
        )
    else:
        test_proof = False

    # 7. Check Artifact Presence
    expected_artifacts = extract_expected_artifact_paths(quest)
    artifacts_checked = []
    search_root = wt_p if wt_p and wt_p.exists() else git_ops.get_repo_root(cwd)

    for p_str in expected_artifacts:
        candidate = search_root / p_str
        exists = candidate.exists()
        artifacts_checked.append({"path": p_str, "exists": exists})
        if not exists and quest.status in ("TRIBUTE_READY", "GATE"):
            if p_str.startswith("tasks/") or p_str.startswith("tests/"):
                warnings.append(WardWarning(f"Expected artifact path not found on disk: {p_str}"))

    return WardAudit(
        quest=quest,
        worktree_path=str(wt_p) if wt_p else None,
        git_status=git_stat,
        task_progress=task_prog,
        tribute_present=tribute_present,
        present_sections=present_secs,
        missing_sections=missing_secs,
        extracted_sections=extracted,
        test_proof_found=test_proof,
        artifacts_checked=artifacts_checked,
        violations=violations,
        warnings=warnings,
    )


def audit_all_quests(
    app: Optional[str] = None,
    epic: Optional[str] = None,
    status: Optional[str] = None,
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
    """Auto-sync `# Tribute Rendered`, `# Expected Tribute`, and frontmatter from a worktree into master quest file."""
    wt = None
    if worktree_path:
        wt = Path(worktree_path)
    else:
        found_p = git_ops.find_worktree_for_quest(quest, cwd=cwd)
        if found_p:
            wt = found_p

    if not wt or not wt.exists():
        return False, f"No active worktree found on disk for {quest.id}"

    wt_quest_file = wt / ".court" / "quests" / f"{quest.id}.md"
    if not wt_quest_file.exists():
        wt_epic_file = wt / ".court" / "epics" / f"{quest.id}.md"
        if wt_epic_file.exists():
            wt_quest_file = wt_epic_file
        else:
            court_q_dir = wt / ".court" / "quests"
            if court_q_dir.exists():
                short_id = quest.id.split("-")[0]
                matches = list(court_q_dir.glob(f"{short_id}*.md"))
                if matches:
                    wt_quest_file = matches[0]

    if not wt_quest_file.exists():
        return False, f"Quest file not found in worktree: {wt_quest_file}"

    try:
        wt_quest = Quest.from_markdown(wt_quest_file.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"Failed to parse worktree quest markdown: {e}"

    modified = False
    notes = []

    wt_tribute = wt_quest.body_sections.get("Tribute Rendered", "").strip()
    master_tribute = quest.body_sections.get("Tribute Rendered", "").strip()

    if wt_tribute and not _is_placeholder_or_empty(wt_tribute) and wt_tribute != master_tribute:
        quest.body_sections["Tribute Rendered"] = wt_tribute
        modified = True
        notes.append("Synced Tribute Rendered")

    wt_expected = wt_quest.body_sections.get("Expected Tribute", "").strip()
    master_expected = quest.body_sections.get("Expected Tribute", "").strip()
    if wt_expected and not _is_placeholder_or_empty(wt_expected) and wt_expected != master_expected:
        quest.body_sections["Expected Tribute"] = wt_expected
        modified = True
        notes.append("Synced Expected Tribute")

    _legacy_status_aliases = {"REVIEW": "TRIBUTE_READY"}
    _forward_sync_order = {
        "OPEN": 0,
        "PLANNED": 1,
        "CHARTERED": 2,
        "DISPATCHED": 2,
        "QUESTING": 3,
        "WORKING": 3,
        "TRIBUTE_READY": 4,
    }
    wt_status_raw = (wt_quest.status or "").strip()
    wt_status = _legacy_status_aliases.get(wt_status_raw, wt_status_raw)
    current_rank = _forward_sync_order.get(quest.status, -1)
    wt_rank = _forward_sync_order.get(wt_status, -1)
    if (
        quest.status in ("CHARTERED", "DISPATCHED", "QUESTING", "WORKING")
        and wt_rank > current_rank
        and wt_status in STATUSES
    ):
        old_status = quest.status
        legacy_note = f" (worktree copy still used legacy 'REVIEW' name)" if wt_status_raw == "REVIEW" else ""
        quest.set_status(
            wt_status,
            note=f"Auto-synced status advance from worktree's own copy, which had already self-advanced{legacy_note}",
        )
        modified = True
        notes.append(f"Synced status {old_status} -> {wt_status} from worktree")

    if wt_quest.serf_session_id and not quest.serf_session_id:
        quest.serf_session_id = wt_quest.serf_session_id
        modified = True
        notes.append("Synced serf_session_id")
    if wt_quest.serf_model and not quest.serf_model:
        quest.serf_model = wt_quest.serf_model
        modified = True
        notes.append("Synced serf_model")
    if not quest.worktree and wt:
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
    """Realm-wide compliance health summary used by `court ward`."""
    results = audit_all_quests(
        status="WORKING,DISPATCHED,TRIBUTE_READY,GATE",
        include_archive=include_archive,
        base_branch=base_branch,
        court_root=court_root,
    )
    non_compliant = [r for r in results if not r.is_compliant]
    dirty = [r for r in results if r.git_status.get("dirty")]
    drifting = [r for r in results if (r.git_status.get("behind") or 0) > 0]
    missing_tribute = [r for r in results if not r.tribute_present]

    return {
        "total_audited": len(results),
        "total_active": len(results),
        "compliant_count": len(results) - len(non_compliant),
        "non_compliant_count": len(non_compliant),
        "dirty_worktrees_count": len(dirty),
        "dirty_count": len(dirty),
        "drifting_worktrees_count": len(drifting),
        "behind_count": len(drifting),
        "missing_tribute_count": len(missing_tribute),
        "results": results,
        "audits": results,
    }


_WARDEN_SECTION_ALIASES = {
    "survey": "survey",
    "the survey": "survey",
    "stack trace": "stack_trace",
    "stack_trace": "stack_trace",
    "traceback": "stack_trace",
    "error": "stack_trace",
    "the stack trace": "stack_trace",
    "impact": "impact",
    "affected accounts": "impact",
    "affected_accounts": "impact",
    "impact / affected accounts": "impact",
    "the impact": "impact",
    "root cause": "root_cause",
    "root_cause": "root_cause",
    "diagnosis": "root_cause",
    "root cause diagnosis": "root_cause",
    "the root cause": "root_cause",
    "proposed fix": "proposed_fix",
    "proposed_fix": "proposed_fix",
    "remit": "proposed_fix",
    "proposed fix / remit": "proposed_fix",
    "fix": "proposed_fix",
    "the proposed fix": "proposed_fix",
}


def parse_warden_report(path_or_text: str | Path, filepath: Optional[str] = None) -> dict[str, Any]:
    """Parse a standalone 5-part Warden Report markdown document or string."""
    p = Path(path_or_text) if isinstance(path_or_text, Path) else None
    if p and p.is_file():
        try:
            text = p.read_text(encoding="utf-8")
            path_str = str(p)
        except Exception as e:
            return {"path": str(p), "exists": True, "valid": False, "error": f"read error: {e}"}
    elif isinstance(path_or_text, str) and "\n" in path_or_text:
        text = path_or_text
        path_str = filepath or "<string>"
    else:
        candidate = Path(str(path_or_text))
        if candidate.is_file():
            try:
                text = candidate.read_text(encoding="utf-8")
                path_str = str(candidate)
            except Exception as e:
                return {"path": str(candidate), "exists": True, "valid": False, "error": f"read error: {e}"}
        else:
            return {"path": str(path_or_text), "exists": False, "valid": False, "error": "file not found"}

    sections: dict[str, str] = {}
    current_key: Optional[str] = None
    buf: list[str] = []

    for line in text.splitlines():
        m = re.match(r"^#{1,4}\s+(.*)$", line.strip())
        matched_key = None
        if m:
            raw_title = m.group(1).strip()
            raw_title = re.sub(r"^\(?\d+[\.\)]\s*", "", raw_title)
            raw_title = re.sub(r"\s*\(.*?\)\s*$", "", raw_title)
            cleaned = re.sub(r"[^\w\s/_-]", "", raw_title).lower().strip()
            if cleaned in _WARDEN_SECTION_ALIASES:
                matched_key = _WARDEN_SECTION_ALIASES[cleaned]
            else:
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

    present = [s for s in WARDEN_REPORT_SECTIONS if s in sections and not _is_placeholder_or_empty(sections[s])]
    missing = [s for s in WARDEN_REPORT_SECTIONS if s not in present]

    return {
        "path": path_str,
        "exists": True,
        "valid": len(missing) == 0,
        "complete": len(missing) == 0,
        "present": present,
        "present_sections": present,
        "missing_sections": missing,
        "sections": sections,
        "raw_text": text,
    }
