"""
The Warden's Quested Bug Ledger (`court.bug_ledger`).

Provides deterministic tracking, deduplication, and lifecycle management for bugs
that have been quested across the realm. Solves the "Fix-to-Deploy Window" problem:
preventing the Warden from re-alerting or re-questing bugs that continue firing
in production while a fix is in-flight or waiting for deployment.

Lifecycle States (`deploy_status`):
- `IN_FLIGHT`: Bug has been quested (OPEN -> PLANNED -> WORKING -> TRIBUTE_READY -> GATE).
  Fix exists only in worktree/staging. Production occurrences are PRE-DEPLOY NOISE (suppressed).
- `STAGED`: Packed in an ephemeral gatehouse convoy (`the-gatehouse/<cogship_id>`) or
  merged to `castle`. Awaiting production deployment. Production occurrences are PRE-DEPLOY NOISE.
- `DEPLOYED`: Merged to `main` and deployed (`court ship --confirm`).
  Enters observation window. Errors firing AFTER `deployed_at` are TRUE REGRESSIONS.
- `VERIFIED_CLOSED`: Monitored through observation window with zero recurrences;
  error issue confirmed resolved.
"""
from __future__ import annotations

import datetime
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from . import git_ops
    _REPO_ROOT = git_ops.get_repo_root()
except Exception:
    _REPO_ROOT = Path(__file__).resolve().parent.parent

_COURT_DIR = _REPO_ROOT / ".court"
BUG_LEDGER_PATH = _COURT_DIR / "ward" / "BUG_LEDGER.md"

DEPLOY_STATUSES = ("IN_FLIGHT", "STAGED", "DEPLOYED", "VERIFIED_CLOSED")

# Classification verdicts returned to the Warden
VERDICT_PRE_DEPLOY_NOISE = "PRE_DEPLOY_NOISE"
VERDICT_PRE_DEPLOY_REMNANT = "PRE_DEPLOY_REMNANT"
VERDICT_POST_DEPLOY_REGRESSION = "POST_DEPLOY_REGRESSION"
VERDICT_REGRESSION_CLOSED = "REGRESSION_CLOSED"
VERDICT_UNTRACKED_BUG = "UNTRACKED_BUG"


@dataclass
class BugRecord:
    bug_id: str
    quest_id: str = ""
    sentry_short_id: str = ""
    sentry_issue_id: str = ""
    sentry_url: str = ""
    app: str = "platform"
    error_type: str = ""
    logger: str = ""
    signature: str = ""
    quest_status: str = "OPEN"
    deploy_status: str = "IN_FLIGHT"  # IN_FLIGHT | STAGED | DEPLOYED | VERIFIED_CLOSED
    cogship_id: str = ""
    deployed_at: str = ""  # ISO UTC
    deployed_commit: str = ""
    last_seen_production: str = ""  # ISO UTC
    sentry_status: str = "unresolved"  # unresolved | resolved_in_next_release | resolved | ignored
    created_at: str = ""
    updated_at: str = ""
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def is_in_flight(self) -> bool:
        return self.deploy_status in ("IN_FLIGHT", "STAGED") or not self.deployed_at

    @property
    def is_deployed(self) -> bool:
        return self.deploy_status == "DEPLOYED" and bool(self.deployed_at)

    @property
    def is_closed(self) -> bool:
        return self.deploy_status == "VERIFIED_CLOSED"


@dataclass
class ClassificationResult:
    verdict: str
    bug: Optional[BugRecord] = None
    reason: str = ""
    should_suppress: bool = False
    is_regression: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "bug_id": self.bug.bug_id if self.bug else None,
            "quest_id": self.bug.quest_id if self.bug else None,
            "deploy_status": self.bug.deploy_status if self.bug else None,
            "reason": self.reason,
            "should_suppress": self.should_suppress,
            "is_regression": self.is_regression,
        }


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(iso_str: str) -> Optional[datetime.datetime]:
    if not iso_str:
        return None
    try:
        clean = iso_str.strip()
        if clean.endswith("Z"):
            clean = clean[:-1] + "+00:00"
        return datetime.datetime.fromisoformat(clean)
    except Exception:
        return None


def match_bug(
    bugs: List[BugRecord],
    short_id: str = "",
    issue_id: str = "",
    logger: str = "",
    error_type: str = "",
    message: str = "",
    traceback: str = "",
) -> Optional[BugRecord]:
    clean_short_id = short_id.strip().upper() if short_id else ""
    clean_issue_id = issue_id.strip() if issue_id else ""
    clean_logger = logger.strip().lower() if logger else ""
    clean_error_type = error_type.strip().lower() if error_type else ""
    combined_msg = f"{error_type} {message} {traceback}".lower()

    # Pass 1: Sentry ID match
    for b in bugs:
        if clean_short_id and b.sentry_short_id and b.sentry_short_id.upper() == clean_short_id:
            return b
        if clean_issue_id and b.sentry_issue_id and b.sentry_issue_id == clean_issue_id:
            return b

    # Pass 2: Logger + signature / error_type match
    for b in bugs:
        b_logger = b.logger.strip().lower()
        b_sig = b.signature.strip().lower()
        b_err = b.error_type.strip().lower()

        logger_matches = (
            not b_logger
            or not clean_logger
            or b_logger == clean_logger
            or clean_logger.startswith(b_logger)
            or b_logger.startswith(clean_logger)
        )

        if not logger_matches:
            continue

        if b_sig and (b_sig in combined_msg or b_sig in message.lower()):
            return b

        if b_err and clean_error_type and b_err == clean_error_type:
            if b.bug_id.lower() in combined_msg:
                return b

    return None


def classify_error(
    bug: Optional[BugRecord],
    event_time_iso: Optional[str] = None,
) -> ClassificationResult:
    if not bug:
        return ClassificationResult(
            verdict=VERDICT_UNTRACKED_BUG,
            bug=None,
            reason="Error is not tracked in the Quested Bug Ledger. Fresh anomaly requiring investigation.",
            should_suppress=False,
            is_regression=False,
        )

    if bug.is_closed:
        return ClassificationResult(
            verdict=VERDICT_REGRESSION_CLOSED,
            bug=bug,
            reason=f"Bug {bug.bug_id} was marked VERIFIED_CLOSED but recurred in production! True regression.",
            should_suppress=False,
            is_regression=True,
        )

    if bug.is_in_flight:
        stage_desc = f"Quest {bug.quest_id} is in status '{bug.quest_status}' (deploy status: {bug.deploy_status})"
        if bug.cogship_id:
            stage_desc += f", packed in {bug.cogship_id}"
        return ClassificationResult(
            verdict=VERDICT_PRE_DEPLOY_NOISE,
            bug=bug,
            reason=f"Known in-flight bug: {stage_desc}. Fix has not deployed to production yet.",
            should_suppress=True,
            is_regression=False,
        )

    if bug.is_deployed and bug.deployed_at and event_time_iso:
        event_dt = _parse_iso(event_time_iso)
        deploy_dt = _parse_iso(bug.deployed_at)
        if event_dt and deploy_dt:
            if event_dt <= deploy_dt:
                return ClassificationResult(
                    verdict=VERDICT_PRE_DEPLOY_REMNANT,
                    bug=bug,
                    reason=(
                        f"Occurrence timestamp ({event_time_iso}) predates or matches deployment "
                        f"cutover ({bug.deployed_at}). Pre-deploy remnant, safe to ignore."
                    ),
                    should_suppress=True,
                    is_regression=False,
                )
            else:
                return ClassificationResult(
                    verdict=VERDICT_POST_DEPLOY_REGRESSION,
                    bug=bug,
                    reason=(
                        f"CRITICAL: Error fired at {event_time_iso}, which is AFTER deployment cutover "
                        f"at {bug.deployed_at} (commit {bug.deployed_commit or 'unknown'})! "
                        f"Fix in Quest {bug.quest_id} was ineffective or regressed."
                    ),
                    should_suppress=False,
                    is_regression=True,
                )

    if bug.deploy_status == "DEPLOYED":
        return ClassificationResult(
            verdict=VERDICT_POST_DEPLOY_REGRESSION,
            bug=bug,
            reason=f"Error occurred on bug {bug.bug_id} with deploy status DEPLOYED. Treat as post-deploy regression.",
            should_suppress=False,
            is_regression=True,
        )

    return ClassificationResult(
        verdict=VERDICT_PRE_DEPLOY_NOISE,
        bug=bug,
        reason=f"Bug {bug.bug_id} has active quest {bug.quest_id}. Suppressing pre-deploy alerts.",
        should_suppress=True,
        is_regression=False,
    )


_ENTRY_HEADING_RE = re.compile(r"^###\s+\[(.*?)\]\s*(.*)$")
_FIELD_LINE_RE = re.compile(r"^\s*-\s+\*\*(.*?)(?::\*\*|\*\*:\s*)\s*(.*)$")


def parse_bug_ledger_text(text: str) -> List[BugRecord]:
    records: List[BugRecord] = []
    current: Optional[Dict[str, str]] = None
    current_id: str = ""

    for line in text.splitlines():
        hm = _ENTRY_HEADING_RE.match(line.strip())
        if hm:
            if current and current_id:
                records.append(_dict_to_bug_record(current_id, current))
            current_id = hm.group(1).strip()
            title_desc = hm.group(2).strip()
            current = {"_title": title_desc}
            continue

        if current is not None:
            fm = _FIELD_LINE_RE.match(line.strip())
            if fm:
                key = fm.group(1).strip().lower().replace(" ", "_")
                val = fm.group(2).strip()
                current[key] = val

    if current and current_id:
        records.append(_dict_to_bug_record(current_id, current))

    return records


def _dict_to_bug_record(bug_id: str, data: Dict[str, str]) -> BugRecord:
    deploy_status = data.get("deploy_status", "IN_FLIGHT").strip().upper()
    if deploy_status not in DEPLOY_STATUSES:
        deploy_status = "IN_FLIGHT"

    return BugRecord(
        bug_id=bug_id,
        quest_id=data.get("quest_id", ""),
        sentry_short_id=data.get("sentry_short_id", ""),
        sentry_issue_id=data.get("sentry_issue_id", ""),
        sentry_url=data.get("sentry_url", ""),
        app=data.get("app", "platform"),
        error_type=data.get("error_type", ""),
        logger=data.get("logger", ""),
        signature=data.get("signature", ""),
        quest_status=data.get("quest_status", "OPEN").upper(),
        deploy_status=deploy_status,
        cogship_id=data.get("cogship_id", ""),
        deployed_at=data.get("deployed_at", ""),
        deployed_commit=data.get("deployed_commit", ""),
        last_seen_production=data.get("last_seen_production", ""),
        sentry_status=data.get("sentry_status", "unresolved").lower(),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        notes=data.get("notes", data.get("_title", "")),
    )


def serialize_bug_ledger(records: List[BugRecord], last_updated: Optional[str] = None) -> str:
    ts = last_updated or _now_iso()
    counts = {s: 0 for s in DEPLOY_STATUSES}
    for r in records:
        st = r.deploy_status if r.deploy_status in counts else "IN_FLIGHT"
        counts[st] += 1

    lines = [
        "# The Warden's Quested Bug Ledger",
        "",
        "> Durable registry of bugs quested across the realm. Used by the Warden to",
        "> distinguish between (1) in-flight pre-deploy noise, (2) true post-deploy regressions,",
        "> and (3) untracked fresh errors. Solves the Fix-to-Deploy Window gap.",
        "",
        f"**Last Updated:** `{ts}`  ",
        f"**Tracked Bugs:** `{len(records)}` | **In-Flight:** `{counts['IN_FLIGHT']}` | "
        f"**Staged:** `{counts['STAGED']}` | **Deployed:** `{counts['DEPLOYED']}` | "
        f"**Closed:** `{counts['VERIFIED_CLOSED']}`",
        "",
        "---",
        "",
        "## Quick Reference",
        "",
        "| Bug ID | Sentry Short ID | Quest ID | App | Quest Status | Deploy Status | Cogship | Deployed At |",
        "|---|---|---|---|---|---|---|---|",
    ]

    for r in records:
        short = r.sentry_short_id or "-"
        q_id = r.quest_id or "-"
        cog = r.cogship_id or "-"
        dep_at = r.deployed_at or "-"
        lines.append(
            f"| `{r.bug_id}` | {short} | `{q_id}` | `{r.app}` | `{r.quest_status}` | "
            f"`{r.deploy_status}` | `{cog}` | {dep_at} |"
        )

    lines.extend([
        "",
        "---",
        "",
        "## Detailed Bug Registry",
        "",
    ])

    for r in records:
        title = r.notes or f"{r.app} {r.error_type}"
        lines.extend([
            f"### [{r.bug_id}] {title}",
            f"- **Quest ID**: {r.quest_id}",
            f"- **Sentry Short ID**: {r.sentry_short_id}",
            f"- **Sentry Issue ID**: {r.sentry_issue_id}",
            f"- **Sentry URL**: {r.sentry_url}",
            f"- **App**: {r.app}",
            f"- **Error Type**: {r.error_type}",
            f"- **Logger**: {r.logger}",
            f"- **Signature**: {r.signature}",
            f"- **Quest Status**: {r.quest_status}",
            f"- **Deploy Status**: {r.deploy_status}",
            f"- **Cogship ID**: {r.cogship_id}",
            f"- **Deployed At**: {r.deployed_at}",
            f"- **Deployed Commit**: {r.deployed_commit}",
            f"- **Last Seen Production**: {r.last_seen_production}",
            f"- **Sentry Status**: {r.sentry_status}",
            f"- **Created At**: {r.created_at}",
            f"- **Updated At**: {r.updated_at}",
            f"- **Notes**: {r.notes}",
            "",
        ])

    return "\n".join(lines).strip() + "\n"


def load_bug_ledger(path: Optional[Path] = None) -> List[BugRecord]:
    p = path or BUG_LEDGER_PATH
    if not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8")
        return parse_bug_ledger_text(text)
    except Exception:
        return []


def save_bug_ledger(records: List[BugRecord], path: Optional[Path] = None) -> Path:
    p = path or BUG_LEDGER_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    content = serialize_bug_ledger(records)
    p.write_text(content, encoding="utf-8")
    return p


def record_or_update_bug(
    record: BugRecord,
    path: Optional[Path] = None,
) -> BugRecord:
    records = load_bug_ledger(path)
    now = _now_iso()
    if not record.created_at:
        record.created_at = now
    record.updated_at = now

    updated = False
    for i, r in enumerate(records):
        if r.bug_id == record.bug_id or (record.sentry_short_id and r.sentry_short_id == record.sentry_short_id):
            records[i] = record
            updated = True
            break

    if not updated:
        records.append(record)

    save_bug_ledger(records, path)
    return record


def mark_convoy_deployed(
    cogship_id: str,
    quest_ids: List[str],
    deployed_commit: str = "",
    deployed_at: Optional[str] = None,
    path: Optional[Path] = None,
) -> List[BugRecord]:
    records = load_bug_ledger(path)
    now = deployed_at or _now_iso()
    quest_id_set = {q.strip() for q in quest_ids if q.strip()}
    modified: List[BugRecord] = []

    for r in records:
        if r.quest_id in quest_id_set or r.cogship_id == cogship_id:
            r.deploy_status = "DEPLOYED"
            r.deployed_at = now
            if deployed_commit:
                r.deployed_commit = deployed_commit
            r.updated_at = now
            modified.append(r)

    if modified:
        save_bug_ledger(records, path)
    return modified


def sync_from_quests(
    all_quests: List[Any],
    path: Optional[Path] = None,
) -> Tuple[int, int]:
    existing_records = {r.bug_id: r for r in load_bug_ledger(path)}
    short_id_map = {r.sentry_short_id: r for r in existing_records.values() if r.sentry_short_id}
    quest_id_map = {r.quest_id: r for r in existing_records.values() if r.quest_id}

    created = 0
    updated = 0
    now = _now_iso()

    for q in all_quests:
        is_bug = (
            q.section == "Bug fix"
            or "bug fix" in (q.tags or "").lower()
            or "sentry" in (q.tags or "").lower()
            or "pb-app-" in q.title.lower()
            or "pb-app-" in q.concern.lower()
        )
        if not is_bug:
            continue

        sentry_short_id = ""
        m_short = re.search(r"\b(PB-APP-[A-Z0-9]+)\b", f"{q.title} {q.tags} {q.concern}", re.I)
        if m_short:
            sentry_short_id = m_short.group(1).upper()

        sentry_issue_id = ""
        sentry_url = ""
        logger = ""
        error_type = ""
        req_text = q.body_sections.get("The Kingdom Requires", "") if hasattr(q, "body_sections") else ""

        m_id = re.search(r"Sentry Issue ID\**:\s*(\d+)", req_text, re.I)
        if m_id:
            sentry_issue_id = m_id.group(1)

        m_url = re.search(r"(https?://(?:picobarn\.)?sentry\.io/issues/\d+/?)", req_text, re.I)
        if m_url:
            sentry_url = m_url.group(1)
            if not sentry_issue_id:
                m_url_id = re.search(r"/issues/(\d+)/?", sentry_url)
                if m_url_id:
                    sentry_issue_id = m_url_id.group(1)

        m_log = re.search(r"Logger\**:\s*`?([a-zA-Z0-9_\.]+)`?", req_text, re.I)
        if m_log:
            logger = m_log.group(1)

        m_err = re.search(r"\b([A-Z][a-zA-Z0-9]+(?:Error|Exception))\b", q.title)
        if m_err:
            error_type = m_err.group(1)

        signature = q.title
        if sentry_short_id and f"[{sentry_short_id}]" in signature:
            signature = signature.split(f"[{sentry_short_id}]", 1)[1].lstrip(": ").strip()

        quest_status = (q.status or "OPEN").upper()
        cogship_id = getattr(q, "cogship_id", "") or ""
        deploy_status = "IN_FLIGHT"

        if quest_status in ("READY_TO_RAZE", "DONE"):
            deploy_status = "DEPLOYED"
        elif cogship_id:
            deploy_status = "STAGED"

        if sentry_short_id:
            bug_id = f"BUG-{sentry_short_id}"
        else:
            bug_id = f"BUG-{q.id.split('-')[0]}-{q.app.upper()}"

        existing = short_id_map.get(sentry_short_id) or quest_id_map.get(q.id) or existing_records.get(bug_id)

        if existing:
            changed = False
            if existing.quest_status != quest_status:
                existing.quest_status = quest_status
                changed = True
            if existing.deploy_status != deploy_status and existing.deploy_status != "VERIFIED_CLOSED":
                existing.deploy_status = deploy_status
                changed = True
            if cogship_id and existing.cogship_id != cogship_id:
                existing.cogship_id = cogship_id
                changed = True
            if sentry_url and not existing.sentry_url:
                existing.sentry_url = sentry_url
                changed = True
            if sentry_issue_id and not existing.sentry_issue_id:
                existing.sentry_issue_id = sentry_issue_id
                changed = True
            if logger and not existing.logger:
                existing.logger = logger
                changed = True
            if error_type and not existing.error_type:
                existing.error_type = error_type
                changed = True
            if changed:
                existing.updated_at = now
                updated += 1
        else:
            record = BugRecord(
                bug_id=bug_id,
                quest_id=q.id,
                sentry_short_id=sentry_short_id,
                sentry_issue_id=sentry_issue_id,
                sentry_url=sentry_url,
                app=q.app or "platform",
                error_type=error_type,
                logger=logger,
                signature=signature[:200],
                quest_status=quest_status,
                deploy_status=deploy_status,
                cogship_id=cogship_id,
                deployed_at="",
                deployed_commit="",
                last_seen_production="",
                sentry_status="unresolved",
                created_at=now,
                updated_at=now,
                notes=q.title,
            )
            existing_records[bug_id] = record
            created += 1

    save_bug_ledger(list(existing_records.values()), path)
    return created, updated


def get_ledger_summary(path: Optional[Path] = None) -> Dict[str, Any]:
    records = load_bug_ledger(path)
    in_flight = [r for r in records if r.deploy_status == "IN_FLIGHT"]
    staged = [r for r in records if r.deploy_status == "STAGED"]
    deployed = [r for r in records if r.deploy_status == "DEPLOYED"]
    closed = [r for r in records if r.deploy_status == "VERIFIED_CLOSED"]

    return {
        "total": len(records),
        "in_flight_count": len(in_flight),
        "staged_count": len(staged),
        "deployed_count": len(deployed),
        "closed_count": len(closed),
        "in_flight_bugs": in_flight,
        "staged_bugs": staged,
        "deployed_bugs": deployed,
        "closed_bugs": closed,
    }
