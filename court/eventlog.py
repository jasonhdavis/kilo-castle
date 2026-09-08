"""
Append-only, git-union-mergeable event log backing for Quest/Epic ledger state.

Storage model: `<id>.md` is a derived, fully regenerated view. The append-only
`<id>.events.jsonl` next to it is the actual source of truth. `save()`, `load()`,
and `list_all()` read and write through `eventlog.py`, folding the event log
into a `Quest` on every read and appending only the fields/sections that
changed on every write, instead of rewriting the whole record in place. This
allows two branches' independent edits to the same Quest to merge without
git conflicts under `merge=union` (see repo-root `.gitattributes`).
"""
from __future__ import annotations

import json
from dataclasses import fields as dc_fields
from pathlib import Path
from typing import Optional

from .models import Quest, DEFAULT_BODY_SECTIONS, FRONTMATTER_FIELDS

FIELD_PREFIX = "field:"
SECTION_PREFIX = "section:"

_KNOWN_FIELD_NAMES = frozenset(FRONTMATTER_FIELDS)
_KNOWN_SECTION_TARGETS = frozenset(DEFAULT_BODY_SECTIONS)


def events_path_for(md_path: Path) -> Path:
    """Event log lives next to the rendered markdown, same stem: e.g.
    `.court/quests/Q001-App-Concern.md` -> `Q001-App-Concern.events.jsonl`."""
    return md_path.parent / f"{md_path.stem}.events.jsonl"


def read_events(events_path: Path) -> list[dict]:
    """Read and parse every event line. Skips malformed lines safely."""
    if not events_path.exists():
        return []
    out: list[dict] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and "target" in ev:
            out.append(ev)
    return out


def append_events(events_path: Path, events: list[dict]) -> None:
    """Append-only write: never rewrites or reorders existing lines."""
    if not events:
        return
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")


def fold_events(events: list[dict], quest_id: str, kind: str = "quest") -> Quest:
    """Reduce an ordered event list to a Quest. Last-by-timestamp wins per
    target, with original file position as a tiebreaker when timestamps match."""
    latest: dict[str, tuple[str, int, str]] = {}
    for seq, ev in enumerate(events):
        target = ev.get("target")
        if not isinstance(target, str):
            continue
        ts = str(ev.get("ts", ""))
        value = ev.get("value", "")
        value = "" if value is None else str(value)
        prior = latest.get(target)
        if prior is None or (ts, seq) >= (prior[0], prior[1]):
            latest[target] = (ts, seq, value)

    field_values: dict[str, str] = {}
    section_values: dict[str, str] = {}
    for target, (_, _, value) in latest.items():
        if target.startswith(FIELD_PREFIX):
            name = target[len(FIELD_PREFIX):]
            if name in _KNOWN_FIELD_NAMES:
                field_values[name] = value
        elif target.startswith(SECTION_PREFIX):
            name = target[len(SECTION_PREFIX):]
            if name in _KNOWN_SECTION_TARGETS:
                section_values[name] = value

    known_fields = {f.name for f in dc_fields(Quest) if f.name != "body_sections"}
    kwargs = {k: v for k, v in field_values.items() if k in known_fields and v != ""}
    kwargs["id"] = field_values.get("id") or quest_id
    kwargs.setdefault("kind", kind)
    kwargs.setdefault("title", "")
    return Quest(**kwargs, body_sections=dict(section_values))


def build_events_for_save(quest: Quest, prior: Optional[Quest], ts: str) -> list[dict]:
    """Diff `quest` against `prior` and return one event per changed
    frontmatter field and one event per changed body section."""
    events: list[dict] = []

    for f in FRONTMATTER_FIELDS:
        new_val = str(getattr(quest, f, "") or "")
        old_val = str(getattr(prior, f, "") or "") if prior is not None else ""
        if new_val != old_val:
            events.append({"ts": ts, "target": f"{FIELD_PREFIX}{f}", "value": new_val})

    for section in DEFAULT_BODY_SECTIONS:
        new_val = quest.body_sections.get(section, "") or ""
        old_val = ((prior.body_sections.get(section, "") if prior is not None else "") or "")
        if new_val != old_val:
            events.append({"ts": ts, "target": f"{SECTION_PREFIX}{section}", "value": new_val})

    return events
