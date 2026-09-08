"""
Quest/Epic/Scout markdown model: flat frontmatter + fixed section body, parsed and
serialized without any third-party dependency.

File shape (the "Quest Charter" — the full name of this document; it is the
durable log of the deployment pipeline for one Quest, written by the Steward,
worked against by the Serf):

    ---
    id: Q001
    title: Fix Database Connection Timeout
    kind: quest
    app: api
    concern: db-connection-timeout
    parent_epic:
    section: Bug fix
    status: OPEN
    branch:
    worktree:
    serf_session_id:
    serf_model:
    gatekeeper_session_id:
    gatekeeper_model:
    created_at: 2026-09-02T23:00:00Z
    updated_at: 2026-09-02T23:00:00Z
    ---

    # Q001 — Fix Database Connection Timeout

    ## Castle Ledger
    - **2026-09-02T23:00:00Z** — Quest created

    ## The Kingdom Requires
    ...

    ## Expected Tribute
    ...

    ## Tribute Rendered
    (Written once by the Serf; immutable thereafter — Master of Coin audits
    against this, never edits it.)
    ...

    ## Master of Coin's Audit
    ...

    ## Judgement of the Condemned
    (Only present if Punished.)

    ## Cogship Log
    ...

    ## Audience Log
    (Appended, never overwritten. Holds two things: pending `HELD` decisions
    awaiting M'Lord's judgment, and dated Master-of-Coin-verified interrogation
    entries answered via `/audience {{ id }} "<question>"` — see
    `.court/templates/master_of_coin_interrogate_prompt.md`. Interrogation
    never touches `## Master of Coin's Audit`, `status:`, or the diff.)
    ...

Every section is declarative — short bulleted fields, never data tables —
so both an LLM and M'Lord can always find the same fact in the same spot.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Optional

# Canonical pipeline stages. Failures at TRIBUTE_READY/GATE return to an earlier
# stage (QUESTING/WORKING or CHARTERED/DISPATCHED) rather than inventing new states.
STATUSES = (
    "OPEN",
    "PLANNED",
    "CHARTERED",
    "DISPATCHED",
    "QUESTING",
    "WORKING",
    "TRIBUTE_READY",
    "GATE",
    "READY_TO_RAZE",
    "LANDED",
    "LAUNCHED",
    "DONE",
    "HELD",  # blocked on an Audience decision; not a pipeline failure state
    "PUNISHED",  # Master of Coin rejected the audit; Quest+worktree frozen, successor chartered
    "DEMOTED",  # side-state
)

STATUS_LABELS = {
    "OPEN": "Open",
    "PLANNED": "Planning",
    "CHARTERED": "Chartered",
    "DISPATCHED": "Chartered",
    "QUESTING": "Questing",
    "WORKING": "Questing",
    "TRIBUTE_READY": "Tribute Ready",
    "GATE": "Collecting Tribute",
    "READY_TO_RAZE": "Ready to Raze",
    "LANDED": "Landed on Castle",
    "LAUNCHED": "Launched to Production",
    "DONE": "Landed on Castle",
    "HELD": "Held (side-state)",
    "PUNISHED": "Punished (side-state)",
    "DEMOTED": "Demoted (side-state)",
    # Legacy tokens: old archived Quest files may still carry these on disk
    # (pre-rename). Never written fresh; kept only so status_label() doesn't
    # print a bare/ugly enum string for dead historical records.
    "READY_FOR_TEARDOWN": "Closing Vault (legacy)",
    # Legacy status fallback: archived Quest files may still carry
    # `status: REVIEW` in frontmatter (pre-rename). Never written fresh;
    # kept only so status_label() still renders those old files sensibly.
    "REVIEW": "Tribute Ready (legacy)",
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


KINDS = ("quest", "epic", "scout")

# Agent Manager standard sections / lanes.
SECTIONS = ("Bug fix", "Feature", "Optimization", "Investigation")


def validate_branch_name(branch: str) -> tuple[bool, str]:
    """Validate that branch follows organizational folder hierarchy with slashes."""
    if not branch:
        return True, ""
    if branch in ("main", "castle", "the-gatehouse"):
        return True, ""
    if branch.startswith(("epic/", "quest/", "scout/", "ward/", "the-gatehouse/")):
        return True, ""
    if branch.startswith(("quest-", "scout-", "epic-", "ward-")):
        parts = branch.split("-", 1)
        return False, (
            f"Flat branch name '{branch}' violates folder hierarchy. "
            f"Use organizational folder slash namespace like '{parts[0]}/{parts[1]}' instead."
        )
    return False, (
        f"Branch '{branch}' must use organizational folder prefix ('epic/...', 'quest/...', 'scout/...', 'ward/...', or 'the-gatehouse/...')."
    )


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
    "cogship_id",
    "cogship_station",
    "cogship_promoted_commit",
    "task_file",
    "pillory_of",
    "pilloried_by",
    "scout_of",
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

# Declarative, thematic, in fixed reading order. Every section is short
# bulleted fields, never a data table.
DEFAULT_BODY_SECTIONS = (
    "Castle Ledger",
    "The Kingdom Requires",
    "Expected Tribute",
    "Tribute Rendered",
    "Master of Coin's Audit",
    "Judgement of the Condemned",
    "Cogship Log",
    "Audience Log",
)

# Pre-rename section names. Never written fresh; remapped transparently on
# load so existing Quest/Epic files (active or archived) don't silently lose
# content the next time they're loaded+saved under the new canonical names.
LEGACY_SECTION_ALIASES = {
    "Goal & Scope": "The Kingdom Requires",
    "Master of Coin Review": "Master of Coin's Audit",
    "Gatekeeper Review": "Cogship Log",
    "History": "Castle Ledger",
}

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)

_KNOWN_SECTION_NAMES = frozenset(DEFAULT_BODY_SECTIONS) | frozenset(LEGACY_SECTION_ALIASES)


def detect_body_shape(body_text: str) -> str:
    """Return "new" if the body uses `##` section headers (post-Charter shape),
    "legacy" if it uses single-`#` headers (pre-rename shape).
    """
    canon_hits = 0
    legacy_hits = 0
    saw_double_hash = False
    for line in body_text.splitlines():
        m2 = re.match(r"^##\s+(.+)$", line)
        if m2:
            saw_double_hash = True
            if m2.group(1).strip() in _KNOWN_SECTION_NAMES:
                canon_hits += 1
        else:
            m1 = re.match(r"^#\s+(.+)$", line)
            if m1 and m1.group(1).strip() in _KNOWN_SECTION_NAMES:
                legacy_hits += 1
    if canon_hits and canon_hits >= legacy_hits:
        return "new"
    if legacy_hits:
        return "legacy"
    return "new" if saw_double_hash else "legacy"


def now_iso() -> str:
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
    cogship_id: str = ""
    cogship_station: str = ""
    cogship_promoted_commit: str = ""
    task_file: str = ""
    pillory_of: str = ""  # set on a successor Quest: id of the Punished predecessor it replaces
    pilloried_by: str = ""  # set on a Punished Quest: id of the successor Quest chartered in its place
    scout_of: str = ""  # set on a production Quest: id of the source Scout spike it implements
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
        fm_lines = []
        for f in FRONTMATTER_FIELDS:
            value = getattr(self, f, "")
            fm_lines.append(f"{f}: {value}")
        frontmatter = "---\n" + "\n".join(fm_lines) + "\n---\n"
        title_line = f"# {self.id} — {self.title}\n"
        body_parts = []
        for section in DEFAULT_BODY_SECTIONS:
            content = self.body_sections.get(section, "").rstrip()
            body_parts.append(f"## {section}\n\n{content}\n" if content else f"## {section}\n")
        return frontmatter + "\n" + title_line + "\n" + "\n".join(body_parts)

    @classmethod
    def from_markdown(cls, text: str) -> "Quest":
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

        is_new_shape = detect_body_shape(body_text) == "new"
        header_re = re.compile(r"^##\s+(.+)$") if is_new_shape else re.compile(r"^#\s+(.+)$")

        sections = {}
        current = None
        buf = []
        for line in body_text.splitlines():
            header_m = header_re.match(line)
            if header_m and header_m.group(1).strip() in _KNOWN_SECTION_NAMES:
                if current is not None:
                    sections[current] = "\n".join(buf).strip("\n")
                current = header_m.group(1).strip()
                buf = []
            else:
                buf.append(line)
        if current is not None:
            sections[current] = "\n".join(buf).strip("\n")

        # Transparently migrate pre-rename section names
        for legacy, canonical in LEGACY_SECTION_ALIASES.items():
            if legacy in sections:
                legacy_content = sections.pop(legacy)
                if sections.get(canonical, "").strip():
                    sections[canonical] = sections[canonical].rstrip() + "\n\n" + legacy_content
                else:
                    sections[canonical] = legacy_content

        known_fields = {f.name for f in fields(cls) if f.name != "body_sections"}
        kwargs = {k: v for k, v in data.items() if k in known_fields}
        return cls(**kwargs, body_sections=sections)

    def log_ledger(self, from_status: str, to_status: str, note: str = "") -> None:
        """Append one short bulleted entry to Castle Ledger."""
        existing = self.body_sections.get("Castle Ledger", "")
        ts = now_iso()
        if from_status and to_status and from_status != to_status:
            bullet = f"- **{ts}** — {from_status} → {to_status}" + (f": {note}" if note else "")
        else:
            bullet = f"- **{ts}** — {note}" if note else f"- **{ts}** — {to_status or from_status or 'note'}"
        self.body_sections["Castle Ledger"] = (existing.rstrip() + "\n" + bullet).strip()
        self.updated_at = ts

    def append_history(self, from_status: str, to_status: str, note: str = "") -> None:
        self.log_ledger(from_status, to_status, note)

    def set_status(self, new_status: str, note: str = "") -> None:
        if new_status not in STATUSES:
            raise ValueError(f"Unknown status {new_status!r}; must be one of {STATUSES}")
        old = self.status
        self.status = new_status
        self.log_ledger(old, new_status, note)

    def set_section(self, section_name: str, content: str, mode: str = "replace") -> None:
        if section_name not in DEFAULT_BODY_SECTIONS:
            raise ValueError(f"Unknown section {section_name!r}")
        if mode == "append":
            existing = self.body_sections.get(section_name, "")
            self.body_sections[section_name] = (existing.rstrip() + "\n\n" + content).strip()
        else:
            self.body_sections[section_name] = content.strip()
        self.updated_at = now_iso()

    def extract_tribute_subsection(self, target_section: str) -> str:
        """Extract a specific subsection from Tribute Rendered:
        - Serf Reports: ballad, tribute, tally, penance, audience, opinion
        - Scout Reports: survey, map, dangers, tribute, plot
        """
        raw = self.body_sections.get("Tribute Rendered", "").strip()
        if not raw:
            return ""

        canonical_map = {
            "ballad": "ballad",
            "the ballad": "ballad",
            "tribute": "tribute",
            "the tribute": "tribute",
            "tally": "tally",
            "the tally": "tally",
            "verification": "tally",
            "the verification": "tally",
            "verification runbook": "tally",
            "verification paths": "tally",
            "production verification": "tally",
            "ui verification": "tally",
            "how to verify": "tally",
            "penance": "penance",
            "atone": "penance",
            "the penance": "penance",
            "audience": "audience",
            "the audience": "audience",
            "opinion": "opinion",
            "humble opinion": "opinion",
            "humble_opinion": "opinion",
            "the humble opinion": "opinion",
            "survey": "survey",
            "the survey": "survey",
            "map": "map",
            "the map": "map",
            "dangers": "dangers",
            "the dangers": "dangers",
            "plot": "plot",
            "the plot": "plot",
            "summary": "survey",
        }
        target = target_section.lower().strip()
        target_canonical = canonical_map.get(target, target)

        subsections: dict[str, str] = {}
        current_key = None
        buf: list[str] = []

        for line in raw.splitlines():
            m = re.match(r"^#{1,4}\s+(.*)$", line.strip())
            matched_key = None
            if m:
                raw_title = m.group(1).strip()
                raw_title = re.sub(r"^\(?\d+[\.\)]\s*", "", raw_title)
                raw_title = re.sub(r"\s*\(.*?\)\s*$", "", raw_title)
                cleaned = re.sub(r"[^\w\s_-]", "", raw_title).lower().strip()
                if cleaned in canonical_map:
                    matched_key = canonical_map[cleaned]
                else:
                    for k, v in canonical_map.items():
                        if cleaned == k or cleaned.startswith(k + " ") or cleaned.endswith(" " + k):
                            matched_key = v
                            break

            if matched_key:
                if current_key is not None:
                    subsections[current_key] = "\n".join(buf).strip()
                current_key = matched_key
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

    def extract_commutation(self) -> str:
        """Extract post-deployment commutation instructions from Master of Coin's Audit
        or Tribute Rendered. Returns a string or empty string."""
        def _clean(val: str) -> str:
            val = val.strip()
            if not val or _is_placeholder(val):
                return ""
            if val.lower().rstrip(".") in ("none", "none required", "no commutation required", "none needed", "none.", "n/a"):
                return ""
            return val

        moc = self.body_sections.get("Master of Coin's Audit", "").strip()
        if moc:
            m = re.search(r"(?:^|\n)\s*[-*]\s+\*\*Commutation:\*\*\s*(.+?)(?=\n\s*[-*]\s+\*\*|\Z)", moc, re.DOTALL | re.IGNORECASE)
            if m:
                res = _clean(m.group(1))
                if res:
                    return res
            m_sec = re.search(r"(?:^|\n)#{2,4}\s*(?:Commutation|Activation\s*\(Commutation\)|Post-Deploy\s*Commutation).*?\n(.*?)(?=\n#{2,4}\s+|\Z)", moc, re.DOTALL | re.IGNORECASE)
            if m_sec:
                res = _clean(m_sec.group(1))
                if res:
                    return res

        raw = self.body_sections.get("Tribute Rendered", "").strip()
        if raw:
            m = re.search(r"(?:^|\n)\s*[-*]\s+\*\*Commutation:\*\*\s*(.+?)(?=\n\s*[-*]\s+\*\*|\Z)", raw, re.DOTALL | re.IGNORECASE)
            if m:
                res = _clean(m.group(1))
                if res:
                    return res
            m_sec = re.search(r"(?:^|\n)#{2,4}\s*(?:Commutation|Activation\s*\(Commutation\)|Post-Deploy\s*Commutation).*?\n(.*?)(?=\n#{2,4}\s+|\Z)", raw, re.DOTALL | re.IGNORECASE)
            if m_sec:
                res = _clean(m_sec.group(1))
                if res:
                    return res

        return ""

    def extract_extra_tribute(self) -> str:
        """Extract unrequested extra tribute findings from Master of Coin's Audit
        or Tribute Rendered. Returns a string or empty string."""
        def _clean(val: str) -> str:
            val = val.strip()
            if not val or _is_placeholder(val):
                return ""
            if val.lower().rstrip(".") in ("none", "none found", "none observed", "none detected", "none.", "n/a"):
                return ""
            return val

        # 1. Check Master of Coin's Audit
        moc = self.body_sections.get("Master of Coin's Audit", "").strip()
        if moc:
            m = re.search(r"(?:^|\n)\s*[-*]\s+\*\*(?:Extra\s+Tribute(?:\s+Not\s+Requested)?|Unrequested\s+Tribute|Scope\s+Smuggling):\*\*\s*(.+?)(?=\n\s*[-*]\s+\*\*|\Z)", moc, re.DOTALL | re.IGNORECASE)
            if m:
                res = _clean(m.group(1))
                if res:
                    return res
            m_sec = re.search(r"(?:^|\n)#{2,4}\s*(?:Extra\s+Tribute(?:\s+Not\s+Requested)?|Unrequested\s+Tribute|Scope\s+Smuggling).*?\n(.*?)(?=\n#{2,4}\s+|\Z)", moc, re.DOTALL | re.IGNORECASE)
            if m_sec:
                res = _clean(m_sec.group(1))
                if res:
                    return res

        # 2. Check Tribute Rendered
        raw = self.body_sections.get("Tribute Rendered", "").strip()
        if raw:
            m = re.search(r"(?:^|\n)\s*[-*]\s+\*\*(?:Extra\s+Tribute(?:\s+Not\s+Requested)?|Unrequested\s+Tribute|Scope\s+Smuggling):\*\*\s*(.+?)(?=\n\s*[-*]\s+\*\*|\Z)", raw, re.DOTALL | re.IGNORECASE)
            if m:
                res = _clean(m.group(1))
                if res:
                    return res
            m_sec = re.search(r"(?:^|\n)#{2,4}\s*(?:Extra\s+Tribute(?:\s+Not\s+Requested)?|Unrequested\s+Tribute|Scope\s+Smuggling).*?\n(.*?)(?=\n#{2,4}\s+|\Z)", raw, re.DOTALL | re.IGNORECASE)
            if m_sec:
                res = _clean(m_sec.group(1))
                if res:
                    return res

        return ""

    def extract_audience(self) -> str:
        """Extract the Audience section from Tribute Rendered."""
        return self.extract_tribute_subsection("audience")

    def has_pending_audience(self) -> bool:
        """Return True if the Serf requested an Audience that requires royal judgment
        and has not been recorded as resolved in Audience Log or Castle Ledger."""
        if self.status == "HELD":
            return True
        aud = self.extract_audience()
        if not aud or _is_placeholder(aud):
            return False
        cleaned = aud.strip().lower().rstrip(".")
        if cleaned in ("none", "none required", "none outstanding", "no audience required", "no audience requested", "n/a"):
            return False
        aud_log = self.body_sections.get("Audience Log", "").strip()
        if aud_log and not _is_placeholder(aud_log):
            return False
        return True


def _is_placeholder(text: str) -> bool:
    trimmed = text.strip()
    if not trimmed:
        return True
    placeholder_patterns = [
        re.compile(r"^\s*\[?(?:pending|todo|tbd|none yet|tribute rendered)\]?\s*$", re.I),
        re.compile(r"^\s*<[^>]+>\s*$", re.I),
        re.compile(r"^\s*<!--.*?-->\s*$", re.DOTALL),
    ]
    for pat in placeholder_patterns:
        if pat.match(trimmed):
            return True
    return False
