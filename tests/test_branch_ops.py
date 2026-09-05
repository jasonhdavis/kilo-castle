from pathlib import Path

from court import branch_ops


def test_resolve_canonical_branch_name_trunks():
    assert branch_ops.resolve_canonical_branch_name("castle") == ("castle", "trunk")
    assert branch_ops.resolve_canonical_branch_name("main") == ("main", "trunk")


def test_resolve_canonical_branch_name_gatehouse_flat_to_slash():
    canonical, reason = branch_ops.resolve_canonical_branch_name("the-gatehouse-north")
    assert canonical == "the-gatehouse/north"
    assert reason == "gatehouse_flat_to_slash"


def test_resolve_canonical_branch_name_gatehouse_already_canonical():
    canonical, reason = branch_ops.resolve_canonical_branch_name("the-gatehouse/south")
    assert canonical == "the-gatehouse/south"
    assert reason == "canonical_gatehouse"


def test_resolve_canonical_branch_name_standalone_quest_regex_fallback(tmp_path):
    # No matching Court record on disk (no .court/ under tmp_path) -> falls
    # back to the pure regex-derived canonical name.
    canonical, reason = branch_ops.resolve_canonical_branch_name(
        "quest-q084-some-slug", cwd=tmp_path
    )
    assert canonical == "quest/q084-some-slug"
    assert reason == "regex_standalone_quest"


def test_resolve_canonical_branch_name_standalone_scout_regex_fallback(tmp_path):
    canonical, reason = branch_ops.resolve_canonical_branch_name(
        "scout-q074-some-spike", cwd=tmp_path
    )
    assert canonical == "scout/q074-some-spike"
    assert reason == "regex_standalone_scout"


def test_resolve_canonical_branch_name_epic_regex_fallback(tmp_path):
    canonical, reason = branch_ops.resolve_canonical_branch_name(
        "epic-q012-some-initiative", cwd=tmp_path
    )
    assert canonical == "epic/q012-some-initiative"
    assert reason == "regex_epic"


def test_resolve_canonical_branch_name_epic_child_quest_regex_fallback(tmp_path):
    canonical, reason = branch_ops.resolve_canonical_branch_name(
        "quest-q014-q088-child-work", cwd=tmp_path
    )
    assert canonical == "quest/q014/q088-child-work"
    assert reason == "regex_child_quest"


def test_resolve_canonical_branch_name_already_canonical_quest(tmp_path):
    canonical, reason = branch_ops.resolve_canonical_branch_name(
        "quest/q084-already-canonical", cwd=tmp_path
    )
    assert canonical == "quest/q084-already-canonical"
    assert reason == "already_canonical"


def test_resolve_canonical_branch_name_uses_court_frontmatter_when_available(tmp_path):
    from court.models import Quest
    from court import store

    court_root = tmp_path / ".court"
    q = Quest(
        id="Q084-Test-Frontmatter",
        title="Frontmatter Branch Quest",
        app="test",
        concern="frontmatter-branch",
        branch="quest/q084-test-frontmatter-full-slug",
    )
    store.save(q, court_root=court_root)

    canonical, reason = branch_ops.resolve_canonical_branch_name(
        "quest-q084-truncated", cwd=tmp_path
    )
    assert canonical == "quest/q084-test-frontmatter-full-slug"
    assert "court_quest_frontmatter" in reason


def test_find_quest_in_court_returns_none_when_missing(tmp_path):
    assert branch_ops.find_quest_in_court("Q999", cwd=tmp_path) is None


def test_find_quest_in_court_finds_by_number(tmp_path):
    from court.models import Quest
    from court import store

    court_root = tmp_path / ".court"
    q = Quest(id="Q001-Test-Lookup", title="Lookup Quest", app="test", concern="lookup")
    store.save(q, court_root=court_root)

    found = branch_ops.find_quest_in_court("1", cwd=tmp_path)
    assert found is not None
    assert found.id == "Q001-Test-Lookup"
