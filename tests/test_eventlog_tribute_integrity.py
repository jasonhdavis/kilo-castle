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
from court.models import Quest, DEFAULT_BODY_SECTIONS


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
    md = TRIBUTE_MD.replace(
        "## Penance\n\nConfidence 9/10.",
        "## Royal Addendum\n\nExtra royal ruling.\n\n## Penance\n\nConfidence 9/10.",
    )
    quest = Quest.from_markdown(md)
    assert quest.body_sections.get("Royal Addendum", "").strip() == "Extra royal ruling."
    # Outside Tribute Rendered, a part-named header keeps its historical
    # boundary behavior: "## Penance" becomes its own section again.
    assert quest.body_sections.get("Penance", "").strip() == "Confidence 9/10."
    assert "## Ballad" in quest.body_sections["Tribute Rendered"]


def test_to_markdown_never_renders_tribute_part_keys():
    quest = _make_quest()
    quest.body_sections["Ballad"] = "misfiled content"
    rendered = quest.to_markdown()
    # The Ballad inside Tribute Rendered is content and must render exactly once;
    # the mis-keyed section must not add a second, top-level rendering.
    assert rendered.count("\n## Ballad\n") == 1
    assert "misfiled content" not in rendered


def test_build_events_never_emits_noncanonical_sections():
    quest = _make_quest()
    quest.body_sections["Ballad"] = "misfiled content"
    events = eventlog.build_events_for_save(quest, None, "2026-09-30T01:10:00Z")
    for ev in events:
        if ev["target"].startswith("section:"):
            assert ev["target"].removeprefix("section:") in DEFAULT_BODY_SECTIONS


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
