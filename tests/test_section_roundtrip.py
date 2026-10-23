"""Section round-trip fidelity for free-text quest bodies.

Regression guards for the engine defect where a nested level-2 heading whose
name collided with a tribute sub-part alias (Tally, Verification, Summary, ...)
was re-keyed into its own `body_sections` entry, which `Quest.to_markdown`
never emits — so the nested heading and every following line were silently
dropped on the next load→save cycle.
"""
import pytest

from court.models import Quest


FM = (
    "---\nid: Q001-Core-Round-Trip\ntitle: Round Trip\napp: Core\n"
    "concern: round-trip\nstatus: OPEN\n---\n\n"
    "# Q001-Core-Round-Trip — Round Trip\n\n"
)


@pytest.mark.parametrize(
    "heading", ["Tally", "Verification", "Summary", "Penance", "Map", "Plot", "Audience"]
)
def test_nested_part_named_heading_is_preserved(heading):
    body = (
        "## The Kingdom Requires\n\n"
        "Intro paragraph.\n\n"
        f"## {heading}\n\n"
        "the important text that must survive\n\n"
        "## Expected Tribute\n\n- [ ] item\n"
    )
    quest = Quest.from_markdown(FM + body)

    # The nested heading is content of its enclosing section, not its own key.
    assert heading not in quest.body_sections
    assert f"## {heading}" in quest.body_sections["The Kingdom Requires"]
    assert "the important text that must survive" in quest.body_sections["The Kingdom Requires"]

    # It survives load→save→load byte-for-byte.
    md1 = quest.to_markdown()
    reparsed = Quest.from_markdown(md1)
    assert reparsed.body_sections["The Kingdom Requires"] == (
        quest.body_sections["The Kingdom Requires"]
    )
    assert "the important text that must survive" in reparsed.to_markdown()


def test_goal_section_round_trip_is_byte_identical():
    goal = (
        "Do the work.\n\n"
        "## Tally\n\n"
        "Steps and details that must survive re-serialisation.\n"
    )
    quest = Quest(id="Q001-Core-Round-Trip", title="Round Trip", app="Core", concern="round-trip")
    quest.set_section("The Kingdom Requires", goal)

    reloaded = Quest.from_markdown(quest.to_markdown())
    assert reloaded.body_sections["The Kingdom Requires"] == goal.strip()

    # Second cycle is a fixpoint (no further drift).
    once = reloaded.to_markdown()
    twice = Quest.from_markdown(once).to_markdown()
    assert once == twice


def test_custom_section_order_is_stable():
    body = (
        "## Castle Ledger\n\n- entry\n\n"
        "## The Kingdom Requires\n\nGoal.\n\n"
        "## Royal Addendum\n\nFree text.\n\n"
        "## Expected Tribute\n\n- [ ] x\n"
    )
    quest = Quest.from_markdown(FM + body)
    md1 = quest.to_markdown()
    md2 = Quest.from_markdown(md1).to_markdown()

    # Stable across a cycle, and the custom section keeps its position.
    assert md1 == md2
    assert md1.index("## Royal Addendum") < md1.index("## Expected Tribute")


def test_miskeyed_tribute_part_key_is_still_never_rendered():
    """Defence in depth: a part-named key injected directly is not emitted."""
    quest = Quest(id="Q001-Core-Round-Trip", title="Round Trip", app="Core", concern="round-trip")
    quest.body_sections["Tally"] = "orphan content"
    quest.set_section("The Kingdom Requires", "Real goal.")
    rendered = quest.to_markdown()
    assert "orphan content" not in rendered
    assert "\n## Tally\n" not in rendered
