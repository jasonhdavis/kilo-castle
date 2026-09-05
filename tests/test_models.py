from court.models import Quest, STATUSES, SECTIONS, KINDS, now_iso


def test_quest_initialization_defaults():
    q = Quest(id="Q001-Platform-Test", title="Test Quest", app="platform", concern="test")
    assert q.status == "OPEN"
    assert q.kind == "quest"
    assert q.app == "platform"
    assert q.concern == "test"
    assert q.tree_branch == "quest/q001-platform-test"
    assert "History" in q.body_sections
    assert "Goal & Scope" in q.body_sections


def test_scout_branch_generation():
    scout = Quest(
        id="Q005-Marketplace-Fingerprint",
        title="Store Fingerprint POC",
        kind="scout",
        app="marketplace",
        concern="fingerprint",
    )
    assert scout.tree_branch == "scout/q005-marketplace-fingerprint"

    investigation_quest = Quest(
        id="Q006-Marketplace-Brand",
        title="Brand Observation",
        section="Investigation",
        app="marketplace",
        concern="brand",
    )
    assert investigation_quest.tree_branch == "scout/q006-marketplace-brand"


def test_epic_and_child_tree_branches():
    epic = Quest(id="Q010-Auth-Migration", title="Auth Migration", kind="epic", app="auth", concern="migration")
    assert epic.tree_branch == "epic/q010-auth-migration"

    child = Quest(
        id="Q011-Auth-Jwt",
        title="JWT Support",
        kind="quest",
        app="auth",
        concern="jwt",
        parent_epic="Q010-Auth-Migration",
    )
    assert child.tree_branch == "quest/q010/q011-auth-jwt"


def test_markdown_serialization_and_deserialization():
    q = Quest(
        id="Q001-Platform-Auth",
        title="Implement Auth",
        kind="quest",
        app="platform",
        concern="auth",
        section="Feature",
        status="WORKING",
        branch="quest/q001-platform-auth",
        worktree="wt-123",
        serf_session_id="ses_abc",
        serf_model="Gemini 3.7 Flash",
    )
    q.set_section("Goal & Scope", "Implement user authentication with JWT.")
    q.set_section("Expected Tribute", "- [ ] Auth service tests pass")

    md = q.to_markdown()
    assert "id: Q001-Platform-Auth" in md
    assert "status: WORKING" in md
    assert "# Goal & Scope\n\nImplement user authentication with JWT." in md

    parsed = Quest.from_markdown(md)
    assert parsed.id == q.id
    assert parsed.title == q.title
    assert parsed.status == "WORKING"
    assert parsed.branch == "quest/q001-platform-auth"
    assert parsed.body_sections["Goal & Scope"] == "Implement user authentication with JWT."
    assert parsed.body_sections["Expected Tribute"] == "- [ ] Auth service tests pass"


def test_scout_report_subsection_extraction():
    q = Quest(id="Q005-Test-Scout", title="Scout Test", kind="scout")
    report = """### 1. The Survey
Feasibility is 9/10. High viability for production.

### 2. The Map
Endpoint: POST /api/v2/extract
Schema: { store_id: str, tags: list[str] }

### 3. The Dangers
Hidden 60 RPM rate limit on upstream supplier.

### 4. The Tribute
- tasks/artifacts/poc_output.json
- tasks/artifacts/mock_fixtures.json

### 5. The Plot
Build ExtractService in apps/marketplace/services/extract_service.py
"""
    q.set_section("Tribute Rendered", report)

    assert "Feasibility is 9/10" in q.extract_tribute_subsection("survey")
    assert "POST /api/v2/extract" in q.extract_tribute_subsection("map")
    assert "Hidden 60 RPM rate limit" in q.extract_tribute_subsection("dangers")
    assert "poc_output.json" in q.extract_tribute_subsection("tribute")
    assert "apps/marketplace/services/extract_service.py" in q.extract_tribute_subsection("plot")


def test_tribute_and_tally_subsection_extraction():
    q = Quest(id="Q002-Test-Tribute", title="Tribute Test")
    report = """### 1. Ballad
Implemented user profile view.

### 2. Tribute
- Modified apps/users/views.py (+45 lines)
- Commits: a1b2c3d
- Tests: 12 passed in 1.2s

### 3. Tally
- Navigate to `/users/profile/`
- Click edit profile and update display name
- Verify new name is rendered on dashboard

### 4. Penance
Skipped avatar upload caching.

### 5. Audience
None required.

### 6. Humble Opinion
Advance to review.
"""
    q.set_section("Tribute Rendered", report)

    assert "Implemented user profile view" in q.extract_tribute_subsection("ballad")
    assert "Modified apps/users/views.py" in q.extract_tribute_subsection("tribute")
    assert "Navigate to `/users/profile/`" in q.extract_tribute_subsection("tally")
    assert "Navigate to `/users/profile/`" in q.extract_tribute_subsection("verification")
    assert "Navigate to `/users/profile/`" in q.extract_tribute_subsection("production verification")
    assert "Skipped avatar upload caching" in q.extract_tribute_subsection("penance")
    assert "None required" in q.extract_tribute_subsection("audience")
    assert "Advance to review" in q.extract_tribute_subsection("opinion")


def test_status_transition_and_history():
    q = Quest(id="Q001-Test-Status", title="Test Status", app="test", concern="status")
    q.set_status("PLANNED", "Ready for dispatch")
    assert q.status == "PLANNED"
    assert "PLANNED" in q.body_sections["History"]

    q.set_status("WORKING", "Serf active")
    assert q.status == "WORKING"
    assert "WORKING" in q.body_sections["History"]


def test_set_section_append_and_replace():
    q = Quest(id="Q001-Test-Section", title="Test Section", app="test", concern="section")
    q.set_section("Goal & Scope", "Line 1")
    assert q.body_sections["Goal & Scope"] == "Line 1"

    q.set_section("Goal & Scope", "Line 2", mode="append")
    assert q.body_sections["Goal & Scope"] == "Line 1\n\nLine 2"

    q.set_section("Goal & Scope", "Replaced", mode="replace")
    assert q.body_sections["Goal & Scope"] == "Replaced"


def test_punished_status_is_recognized():
    assert "PUNISHED" in STATUSES
    q = Quest(id="Q040-Test-Punished", title="Punished Quest", app="test", concern="punished")
    q.set_status("PUNISHED", "Master of Coin rejected this audit")
    assert q.status == "PUNISHED"
    assert "PUNISHED" in q.body_sections["History"]


def test_judgement_of_the_condemned_section_present_by_default():
    q = Quest(id="Q041-Test-Judgement", title="Judgement Quest", app="test", concern="judgement")
    assert "Judgement of the Condemned" in q.body_sections
    # Existing canonical section names must not have been renamed away.
    assert "Goal & Scope" in q.body_sections
    assert "Master of Coin Review" in q.body_sections
    assert "Gatekeeper Review" in q.body_sections


def test_new_frontmatter_fields_roundtrip_through_markdown():
    q = Quest(
        id="Q042-Test-Cogship",
        title="Cog Ship Fields",
        app="test",
        concern="cogship",
        status="PUNISHED",
        cogship_id="cogship-003",
        cogship_station="the-gatehouse/north",
        cogship_promoted_commit="abc1234",
        task_file="tasks/apps/test/plan.md",
        pillory_of="Q039-Test-Predecessor",
        pilloried_by="Q043-Test-Successor",
    )
    q.set_section(
        "Judgement of the Condemned",
        "- **Reason:** Missing tests\n- **Decrees Issued:** Reuse the service layer",
    )

    md = q.to_markdown()
    assert "cogship_id: cogship-003" in md
    assert "cogship_station: the-gatehouse/north" in md
    assert "cogship_promoted_commit: abc1234" in md
    assert "task_file: tasks/apps/test/plan.md" in md
    assert "pillory_of: Q039-Test-Predecessor" in md
    assert "pilloried_by: Q043-Test-Successor" in md
    assert "# Judgement of the Condemned" in md
    assert "Missing tests" in md

    parsed = Quest.from_markdown(md)
    assert parsed.status == "PUNISHED"
    assert parsed.cogship_id == "cogship-003"
    assert parsed.cogship_station == "the-gatehouse/north"
    assert parsed.cogship_promoted_commit == "abc1234"
    assert parsed.task_file == "tasks/apps/test/plan.md"
    assert parsed.pillory_of == "Q039-Test-Predecessor"
    assert parsed.pilloried_by == "Q043-Test-Successor"
    assert "Missing tests" in parsed.body_sections["Judgement of the Condemned"]


def test_validate_branch_name():
    from court.models import validate_branch_name

    assert validate_branch_name("") == (True, "")
    assert validate_branch_name("castle")[0] is True
    assert validate_branch_name("main")[0] is True
    assert validate_branch_name("the-gatehouse/north")[0] is True
    assert validate_branch_name("quest/q001-platform-auth")[0] is True
    assert validate_branch_name("epic/q010-auth-migration")[0] is True
    assert validate_branch_name("scout/q005-marketplace-fingerprint")[0] is True

    ok, err = validate_branch_name("quest-q084-flat-name")
    assert ok is False
    assert "quest/q084-flat-name" in err

    ok, err = validate_branch_name("totally-unrecognized")
    assert ok is False
    assert "organizational folder prefix" in err
