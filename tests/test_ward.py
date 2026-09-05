from pathlib import Path

from court.models import Quest
from court import store, ward


def test_is_placeholder():
    assert ward.is_placeholder("") is True
    assert ward.is_placeholder("   ") is True
    assert ward.is_placeholder("pending") is True
    assert ward.is_placeholder("TBD") is True
    assert ward.is_placeholder("<none>") is True
    assert ward.is_placeholder("Real content describing the work done.") is False


def test_parse_markdown_checklist_counts_and_phase():
    content = """## Phase 1: Setup
- [x] Create scaffolding
- [ ] Wire up config

## Phase 2: Build
- [ ] Implement service
- [~] Cancelled edge case
"""
    result = ward.parse_markdown_checklist(content, filepath="plan.md")
    assert result["total"] == 4
    assert result["completed"] == 1
    assert result["cancelled"] == 1
    assert result["pending"] == 2
    assert result["file"] == "plan.md"
    assert "Phase 1" in result["active_phase"]


def test_check_tribute_sections_serf_quest():
    q = Quest(id="Q050-Test-Tribute", title="Tribute Quest", app="test", concern="tribute")
    report = """### 1. Ballad
Shipped the feature.

### 2. Tribute
- Added tests.

### 3. Tally
- Verify manually.

### 4. Penance
None.

### 5. Audience
None required.

### 6. Humble Opinion
Ship it.
"""
    q.set_section("Tribute Rendered", report)
    present, sections_present, missing, extracted = ward.check_tribute_sections(q)
    assert present is True
    assert missing == []
    assert set(sections_present) == set(ward.SERF_REQUIRED_SECTIONS)


def test_check_tribute_sections_missing_when_empty():
    q = Quest(id="Q051-Test-Empty", title="Empty Tribute Quest", app="test", concern="empty")
    present, sections_present, missing, extracted = ward.check_tribute_sections(q)
    assert present is False
    assert sections_present == []
    assert missing == list(ward.SERF_REQUIRED_SECTIONS)


def test_check_tribute_sections_scout_quest():
    q = Quest(id="Q052-Test-Scout", title="Scout Quest", kind="scout", app="test", concern="scout")
    report = """### 1. The Survey
Feasible.

### 2. The Map
Endpoint sketch.

### 3. The Dangers
Rate limits.

### 4. The Tribute
- artifact.json

### 5. The Plot
Build the service.
"""
    q.set_section("Tribute Rendered", report)
    present, sections_present, missing, extracted = ward.check_tribute_sections(q)
    assert present is True
    assert missing == []
    assert set(sections_present) == set(ward.SCOUT_REQUIRED_SECTIONS)


def test_extract_expected_artifact_paths():
    q = Quest(id="Q053-Test-Artifacts", title="Artifacts Quest", app="test", concern="artifacts")
    q.set_section("Expected Tribute", "Add tests in `tests/test_thing.py` and update `apps/thing/service.py`.")
    paths = ward.extract_expected_artifact_paths(q)
    assert "tests/test_thing.py" in paths
    assert "apps/thing/service.py" in paths


def test_audit_quest_open_quest_no_worktree_no_branch_violation(tmp_path):
    court_root = tmp_path / ".court"
    q = Quest(id="Q054-Test-Audit", title="Audit Quest", app="test", concern="audit", status="OPEN")
    store.save(q, court_root=court_root)

    audit = ward.audit_quest(q, base_branch="castle", court_root=court_root)
    assert audit.quest_id == "Q054-Test-Audit"
    # No branch set at all -> blocking violation regardless of status.
    assert any("missing 'branch'" in v.lower() for v in audit.violations)


def test_audit_quest_compliant_when_branch_present_and_open():
    q = Quest(
        id="Q055-Test-Compliant",
        title="Compliant Quest",
        app="test",
        concern="compliant",
        status="OPEN",
        branch="quest/q055-test-compliant",
    )
    audit = ward.audit_quest(q, base_branch="castle")
    assert audit.is_compliant is True
    assert audit.violations == []


def test_discover_worktree_task_files_generic_and_apps_conditional(tmp_path):
    wt = tmp_path / "worktree"
    (wt / "tasks").mkdir(parents=True)
    (wt / "tasks" / "ACTIVE.md").write_text("# Active\n", encoding="utf-8")

    files = ward.discover_worktree_task_files(wt)
    assert any(p.name == "ACTIVE.md" for p in files)

    # tasks/apps/<app> heuristic only activates when tasks/apps/ actually exists.
    apps_dir = wt / "tasks" / "apps" / "myapp"
    apps_dir.mkdir(parents=True)
    (apps_dir / "2026-01-01_plan.md").write_text("# Plan\n", encoding="utf-8")

    files2 = ward.discover_worktree_task_files(wt, app="myapp")
    assert any(p.name == "2026-01-01_plan.md" for p in files2)


def test_parse_warden_report():
    text = """## Survey
Something broke.

## Stack Trace
Traceback here.

## Impact
A few users affected.

## Root Cause Diagnosis
Null pointer.

## Proposed Fix
Add a guard clause.
"""
    result = ward.parse_warden_report(text, filepath="report.md")
    assert result["complete"] is True
    assert set(result["present"]) == set(ward.WARDEN_REPORT_SECTIONS)
    assert "Something broke" in result["sections"]["survey"]
