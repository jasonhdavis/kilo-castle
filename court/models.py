"""
Quest/Epic/Scout markdown model: flat frontmatter + fixed section body, parsed and
serialized without third-party dependencies (stdlib only).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Optional

# Canonical pipeline stages. Rejections return to WORKING or DISPATCHED.
STATUSES = (
    "OPEN",
    "PLANNED",
    "DISPATCHED",
    "WORKING",
    "REVIEW",
    "GATE",
    "READY_FOR_TEARDOWN",
    "DONE",
    "HELD",  # Blocked on an Audience decision; not a pipeline failure state.
)

KINDS = ("quest", "epic", "scout")

# Agent Manager standard sections / lanes.
SECTIONS = ("Bug fix", "Feature", "Optimization", "Investigation")

# Frontmatter field order on disk.
FRONTMATTER_FIELDS = (
    "id",
    "title",
    "kind",
    "app",
    "concern",
    "parent_epic",
    "section",
    "tags",
    "status",
    "branch",
    "worktree",
    "serf_session_id",
    "serf_model",
    "master_of_coin_session_id",
    "master_of_coin_model",
    "gatekeeper_session_id",
    "gatekeeper_model",
    "vassal_session_id",
    "created_at",
    "updated_at",
)

DEFAULT_BODY_SECTIONS = (
    "Goal & Scope",
    "Expected Tribute",
    "Tribute Rendered",
    "Master of Coin Review",
    "Gatekeeper Review",
    "Audience Log",
    "History",
)

HISTORY_HEADER = "| ts | from | to | note |\n|---|---|---|---|\n"

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Quest:
    id: str
    title: str
    kind: str = "quest"
    app: str = ""
    concern: str = ""
    parent_epic: str = ""
    section: str = ""
    tags: str = ""
    status: str = "OPEN"
    branch: str = ""
    worktree: str = ""
    serf_session_id: str = ""
    serf_model: str = ""
    master_of_coin_session_id: str = ""
    master_of_coin_model: str = ""
    gatekeeper_session_id: str = ""
    gatekeeper_model: str = ""
    vassal_session_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    body_sections: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = now_iso()
        if not self.updated_at:
            self.updated_at = self.created_at
        if not self.tags and self.section:
            self.tags = self.section
        for section in DEFAULT_BODY_SECTIONS:
            self.body_sections.setdefault(section, "")
        if not self.body_sections.get("History", "").strip():
            self.body_sections["History"] = HISTORY_HEADER

    @property
    def tree_branch(self) -> str:
        """Standardized tree-structured branch name matching epic/quest/scout hierarchy."""
        short_id = self.id.split("-")[0].lower()
        slug_parts = []
        if self.app:
            slug_parts.append(re.sub(r"[^a-z0-9]+", "-", self.app.lower()).strip("-"))
        if self.concern:
            slug_parts.append(re.sub(r"[^a-z0-9]+", "-", self.concern.lower()).strip("-"))
        slug = "-".join(p for p in slug_parts if p) or "work"

        if self.kind == "epic":
            return f"epic/{short_id}-{slug}"
        if self.kind == "scout" or self.section == "Investigation":
            if self.parent_epic:
                parent_short = self.parent_epic.split("-")[0].lower()
                return f"scout/{parent_short}/{short_id}-{slug}"
            return f"scout/{short_id}-{slug}"
        if self.parent_epic:
            parent_short = self.parent_epic.split("-")[0].lower()
            return f"quest/{parent_short}/{short_id}-{slug}"
        return f"quest/{short_id}-{slug}"

    def to_markdown(self) -> str:
        """Serialize Quest to frontmatter-backed markdown string."""
        fm_lines = []
        for f in FRONTMATTER_FIELDS:
            value = getattr(self, f, "")
            fm_lines.append(f"{f}: {value}")
        frontmatter = "---\n" + "\n".join(fm_lines) + "\n---\n"
        body_parts = []
        for section in DEFAULT_BODY_SECTIONS:
            content = self.body_sections.get(section, "").rstrip()
            body_parts.append(f"# {section}\n\n{content}\n" if content else f"# {section}\n")
        return frontmatter + "\n" + "\n".join(body_parts)

    @classmethod
    def from_markdown(cls, text: str) -> "Quest":
        """Parse Quest from markdown text containing YAML frontmatter and markdown sections."""
        m = _FRONTMATTER_RE.match(text)
        if not m:
            raise ValueError("Malformed quest file: no frontmatter block found")
        fm_text, body_text = m.group(1), m.group(2)
        data = {}
        for line in fm_text.splitlines():
            if not line.strip() or ":" not in line:
                continue
            key, _, value = line.partition(":")
            data[key.strip()] = value.strip()

        # Split body into "# Section" blocks
        sections = {}
        current = None
        buf = []
        for line in body_text.splitlines():
            header_m = re.match(r"^#\s+(.+)$", line)
            if header_m:
                if current is not None:
                    sections[current] = "\n".join(buf).strip("\n")
                current = header_m.group(1).strip()
                buf = []
            else:
                buf.append(line)
        if current is not None:
            sections[current] = "\n".join(buf).strip("\n")

        known_fields = {f.name for f in fields(cls) if f.name != "body_sections"}
        kwargs = {k: v for k, v in data.items() if k in known_fields}
        return cls(**kwargs, body_sections=sections)

    def append_history(self, from_status: str, to_status: str, note: str = "") -> None:
        """Append a state transition record to the History section table."""
        hist = self.body_sections.get("History", "") or HISTORY_HEADER
        if not hist.strip():
            hist = HISTORY_HEADER
        row = f"| {now_iso()} | {from_status or '-'} | {to_status or '-'} | {note or ''} |\n"
        self.body_sections["History"] = hist.rstrip("\n") + "\n" + row
        self.updated_at = now_iso()

    def set_status(self, new_status: str, note: str = "") -> None:
        """Update quest pipeline status and log history transition."""
        if new_status not in STATUSES:
            raise ValueError(f"Unknown status {new_status!r}; must be one of {STATUSES}")
        old = self.status
        self.status = new_status
        self.append_history(old, new_status, note)

    def set_section(self, section_name: str, content: str, mode: str = "replace") -> None:
        """Update or append content to a named body section."""
        if section_name not in DEFAULT_BODY_SECTIONS:
            raise ValueError(f"Unknown section {section_name!r}; valid sections: {DEFAULT_BODY_SECTIONS}")
        if mode == "append":
            existing = self.body_sections.get(section_name, "")
            self.body_sections[section_name] = (existing.rstrip() + "\n\n" + content).strip()
        else:
            self.body_sections[section_name] = content.strip()
        self.updated_at = now_iso()

    def extract_tribute_subsection(self, target_section: str) -> str:
        """Extract a specific subsection from Tribute Rendered:
        - Serf Reports: ballad, tribute, penance, audience, opinion
        - Scout Reports: survey, map, dangers, tribute, plot
        """
        raw = self.body_sections.get("Tribute Rendered", "").strip()
        if not raw:
            return ""

        canonical_map = {
            # Serf standard
            "ballad": "ballad",
            "tribute": "tribute",
            "penance": "penance",
            "audience": "audience",
            "opinion": "opinion",
            "humble opinion": "opinion",
            "humble_opinion": "opinion",
            # Scout standard
            "survey": "survey",
            "the survey": "survey",
            "map": "map",
            "the map": "map",
            "dangers": "dangers",
            "the dangers": "dangers",
            "the tribute": "tribute",
            "plot": "plot",
            "the plot": "plot",
            # Cross-mappings
            "ballad": "ballad",
            "summary": "survey",
        }
        target = target_section.lower().strip()
        target_canonical = canonical_map.get(target, target)

        subsections: dict[str, str] = {}
        current_key = None
        buf: list[str] = []

        for line in raw.splitlines():
            m = re.match(r"^#{1,4}\s+(?:\d+[\.\)]\s+)?([A-Za-z\s_-]+)$", line.strip())
            if m:
                heading = m.group(1).lower().strip()
                if heading in canonical_map:
                    if current_key is not None:
                        subsections[current_key] = "\n".join(buf).strip()
                    current_key = canonical_map[heading]
                    buf = []
                    continue
            buf.append(line)

        if current_key is not None:
            subsections[current_key] = "\n".join(buf).strip()

        if target_canonical in subsections:
            return subsections[target_canonical]

        if target_canonical == "tribute" and not subsections:
            return raw

        return ""
