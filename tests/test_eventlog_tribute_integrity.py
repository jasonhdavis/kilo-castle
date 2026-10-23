"""Tribute-integrity guards for the event-fold (cogship-253/Q661/Q688).

Incident (2026-09-30): tribute part headers (## Ballad, ## Tally, ...) inside
`## Tribute Rendered` were parsed as top-level sections; the re-keyed content
was emitted as non-canonical section events (dropped by the fold) alongside an
empty `Tribute Rendered` event (applied last-write-wins), blinding every
subsequent load. Castle-side paperwork saves kept re-emitting the poison.
"""
import json

import pytest

from court import eventlog, store
from court.cli import main
from court.models import Quest


TRIBUTE_MD = """---
id: Q001-Core-Tribute-Integrity
title: Tribute Integrity Fixture
status: WORKING
---

# Q001-Core-Tribute-Integrity — Tribute Integrity Fixture

## The Kingdom Requires

Do the thing.

## Tribute Rendered

## Ballad

Narrative here.

## Tribute

Files changed list.

## Penance

Confidence 9/10.
"""


def _make_quest():
    quest = Quest.from_markdown(TRIBUTE_MD)
    quest.set_section("Tribute Rendered", quest.body_sections["Tribute Rendered"])
    return quest


def test_from_markdown_keeps_tribute_parts_inline():
    quest = Quest.from_markdown(TRIBUTE_MD)
    for part in ("Ballad", "Tribute", "Penance", "Audience", "Humble Opinion", "Tally"):
        assert part not in quest.body_sections
    tribute = quest.body_sections["Tribute Rendered"]
    assert "## Ballad" in tribute and "Narrative here." in tribute
    assert "## Penance" in tribute and "Confidence 9/10." in tribute


def test_from_markdown_still_preserves_custom_sections():
    # A genuine top-level custom section (Royal Addendum lives after the last
    # canonical section in real charters) survives as its own section.
    md = TRIBUTE_MD + "\n## Cogship Log\n\n## Royal Addendum\n\nExtra royal ruling.\n"
    quest = Quest.from_markdown(md)
    assert "Extra royal ruling." in quest.body_sections["Royal Addendum"]
    assert "## Ballad" in quest.body_sections["Tribute Rendered"]
    # Nothing is lost across a load→save cycle.
    reparsed = Quest.from_markdown(quest.to_markdown())
    assert reparsed.body_sections["Royal Addendum"].strip() == "Extra royal ruling."


def test_custom_heading_inside_freetext_section_is_content():
    """A `## ` heading nested inside a free-text canonical section is CONTENT.

    This is the 2026-10-22 `court dispatch` body-stripping defect: a charter
    goal (`--goal`/`--notes`) whose body carried `## Purpose`/`## Scope`
    sub-headings was split into separate top-level sections; the next
    load→save re-parsed the rendered file, saw The Kingdom Requires as empty,
    and blanked the canonical section on disk — silently deleting the goal and
    M'Lord's notes (pb-app Q877/Q878/Q879/Q888).
    """
    md = (
        "---\nid: Q001-Core-Freetext\ntitle: Free Text\nstatus: PLANNED\n---\n\n"
        "# Q001-Core-Freetext — Free Text\n\n"
        "## The Kingdom Requires\n\n"
        "Goal paragraph.\n\n"
        "## Purpose\n\n"
        "Why this exists.\n\n"
        "## Scope\n\n"
        "- in scope\n\n"
        "M'Lord's Charter Notes: keep this line.\n\n"
        "## Expected Tribute\n\n- [ ] item\n"
    )
    quest = Quest.from_markdown(md)
    goal = quest.body_sections["The Kingdom Requires"]
    assert "## Purpose" in goal and "## Scope" in goal
    assert "M'Lord's Charter Notes: keep this line." in goal
    assert "Purpose" not in quest.body_sections and "Scope" not in quest.body_sections
    # Byte-identical through a load→save→load cycle.
    reparsed = Quest.from_markdown(quest.to_markdown())
    assert reparsed.body_sections["The Kingdom Requires"] == goal


def test_to_markdown_never_renders_tribute_part_keys():
    quest = _make_quest()
    quest.body_sections["Ballad"] = "misfiled content"
    rendered = quest.to_markdown()
    # The Ballad inside Tribute Rendered is content and must render exactly once;
    # the mis-keyed section must not add a second, top-level rendering.
    assert rendered.count("\n## Ballad\n") == 1
    assert "misfiled content" not in rendered


def test_build_events_never_emits_tribute_part_keys():
    quest = _make_quest()
    quest.body_sections["Ballad"] = "misfiled content"
    events = eventlog.build_events_for_save(quest, None, "2026-09-30T01:10:00Z")
    targets = {ev["target"].removeprefix("section:") for ev in events if ev["target"].startswith("section:")}
    assert "Ballad" not in targets
    assert {"Tribute Rendered", "The Kingdom Requires"} <= targets


def test_build_events_persists_custom_sections():
    """Genuine custom sections (Royal Addendum, Context) must survive the fold.

    Dropping them was itself a silent charter-body-stripping defect: a Steward
    addendum written with `set-section "Royal Addendum"` was never re-emitted,
    so the next load lost it.
    """
    quest = _make_quest()
    quest.set_section("Royal Addendum", "Extra royal ruling.")
    events = eventlog.build_events_for_save(quest, None, "2026-10-22T01:10:00Z")
    assert any(
        ev["target"] == "section:Royal Addendum" and ev["value"] == "Extra royal ruling."
        for ev in events
    )


def test_build_events_refuses_accidental_section_wipe():
    prior = _make_quest()
    wiper = Quest(id=prior.id, title=prior.title, body_sections=dict(prior.body_sections))
    wiper.body_sections["Tribute Rendered"] = ""
    events = eventlog.build_events_for_save(wiper, prior, "2026-09-30T01:10:00Z")
    assert not any(e["target"] == "section:Tribute Rendered" for e in events)


def test_build_events_allows_explicit_section_wipe():
    prior = _make_quest()
    wiper = Quest(id=prior.id, title=prior.title, body_sections=dict(prior.body_sections))
    wiper.set_section("Tribute Rendered", "")
    events = eventlog.build_events_for_save(wiper, prior, "2026-09-30T01:10:00Z")
    assert any(
        e["target"] == "section:Tribute Rendered" and e["value"] == ""
        for e in events
    )


def _init_git_repo(repo_dir, branch="castle"):
    import subprocess

    subprocess.run(["git", "init", "-b", branch, str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "t@t"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "t"], check=True, capture_output=True)
    (repo_dir / ".gitattributes").write_text(".court/**/*.events.jsonl merge=union\n")
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "init"], check=True, capture_output=True)


def _poison_events(quest_id, court_dir, quest_title="Blind Fixture"):
    """Append the exact poison signature from the incident: an empty canonical
    tribute event plus full content under mis-keyed tribute-part targets. The
    poison carried the newest timestamp at write time. The ts sits between the
    historical seed and the repair so it wins the fold, then loses to the repair."""
    ev_path = court_dir / "quests" / f"{quest_id}.events.jsonl"
    with ev_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "2026-09-30T00:41:50Z", "target": "field:id", "value": quest_id}) + "\n")
        fh.write(json.dumps({"ts": "2026-09-30T00:41:50Z", "target": "field:title", "value": quest_title}) + "\n")
        fh.write(json.dumps({"ts": "2026-09-30T00:41:50Z", "target": "section:Tribute Rendered", "value": ""}) + "\n")
        fh.write(json.dumps({"ts": "2026-09-30T00:41:50Z", "target": "section:Ballad", "value": "Ballad body"}) + "\n")
        fh.write(json.dumps({"ts": "2026-09-30T00:41:50Z", "target": "section:Tribute", "value": "Tribute body"}) + "\n")
        fh.write(json.dumps({"ts": "2026-09-30T00:41:50Z", "target": "section:Penance", "value": "Penance body"}) + "\n")


def test_charter_goal_with_subheadings_survives_dispatch_save_cycle(tmp_path, monkeypatch, capsys):
    """court new --goal (body carrying `## Purpose`/`## Scope`) → a dispatch-style
    load→mutate→save must NOT blank The Kingdom Requires.

    Reproduces the 2026-10-22 pb-app body-stripping defect: the goal was split
    into top-level sections by the parser, then the next save re-parsed the
    rendered file, saw The Kingdom Requires as empty, and deleted the goal body
    and M'Lord's notes from disk (Q877/Q878/Q879/Q888)."""
    _init_git_repo(tmp_path, branch="castle")
    monkeypatch.setenv("COURT_DIR", str(tmp_path / ".court"))
    monkeypatch.chdir(tmp_path)
    main(["init"])
    capsys.readouterr()

    goal = (
        "Goal paragraph.\n\n"
        "## Purpose\n\nWhy this exists.\n\n"
        "## Scope\n\n- in scope\n\n"
        "M'Lord's Charter Notes: keep this line."
    )
    main(["new", "--app", "core", "--concern", "freetext", "--title", "Free Text", "--goal", goal])
    capsys.readouterr()

    quest = store.load("Q001-Core-Freetext")
    body = quest.body_sections["The Kingdom Requires"]
    assert "## Purpose" in body and "## Scope" in body
    assert "M'Lord's Charter Notes: keep this line." in body
    assert "Purpose" not in quest.body_sections and "Scope" not in quest.body_sections

    # A dispatch-style cycle: load, mutate frontmatter, save.
    quest.branch = "quest/q001-core-freetext"
    store.save(quest, commit_msg="dispatch sim")
    reloaded = store.load("Q001-Core-Freetext")
    assert reloaded.body_sections["The Kingdom Requires"] == body

    rendered = (tmp_path / ".court" / "quests" / "Q001-Core-Freetext.md").read_text(encoding="utf-8")
    assert "## Purpose" in rendered
    assert "M'Lord's Charter Notes: keep this line." in rendered


def test_events_repair_restores_blind_tribute(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path, branch="castle")
    monkeypatch.setenv("COURT_DIR", str(tmp_path / ".court"))
    monkeypatch.chdir(tmp_path)
    main(["init"])

    quest_id = "Q001-Core-Tribute-Integrity"
    quest = _make_quest()
    # Seed with a historical ts so the poison (00:41:50Z, newest at write time
    # in the real incident) deterministically wins the fold.
    quest.updated_at = "2026-09-27T04:45:39Z"
    store.save(quest, commit_msg="seed")
    _poison_events(quest_id, tmp_path / ".court")

    blinded = store.load(quest_id)
    assert blinded.body_sections["Tribute Rendered"] == ""

    main(["events-repair", quest_id])
    out = capsys.readouterr().out
    assert "Repaired" in out
    assert "latest non-empty historical value" in out

    repaired = store.load(quest_id)
    tribute = repaired.body_sections["Tribute Rendered"]
    assert "## Ballad" in tribute and "Narrative here." in tribute
    assert "## Penance" in tribute and "Confidence 9/10." in tribute

    # Re-running is a no-op once the fold is healthy.
    main(["events-repair", quest_id])
    assert "nothing to repair" in capsys.readouterr().out


def test_events_repair_reassembles_from_miskeyed_parts(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path, branch="castle")
    monkeypatch.setenv("COURT_DIR", str(tmp_path / ".court"))
    monkeypatch.chdir(tmp_path)
    main(["init"])

    quest_id = "Q002-Core-Tribute-Integrity"
    quest = Quest(id=quest_id, title="Blind Fixture", app="core", concern="tribute-integrity")
    quest.body_sections["Tribute Rendered"] = ""
    quest.updated_at = "2026-09-27T04:45:39Z"
    store.save(quest, commit_msg="seed")
    _poison_events(quest_id, tmp_path / ".court")

    blinded = store.load(quest_id)
    assert blinded.body_sections["Tribute Rendered"] == ""

    main(["events-repair", quest_id])
    out = capsys.readouterr().out
    assert "reassembled from 3 mis-keyed tribute-part events" in out

    repaired = store.load(quest_id)
    tribute = repaired.body_sections["Tribute Rendered"]
    # Canonical part order: Ballad, Tribute, Tally, Penance, ...
    assert tribute.index("## Ballad") < tribute.index("## Tribute") < tribute.index("## Penance")
    assert "Ballad body" in tribute and "Penance body" in tribute
