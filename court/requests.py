"""Charter requests: the Pleasance lane's only way to ask for new work.

Why this exists (2026-11-09): the remote voice agent used to write Quest
charters straight onto the `pleasance` lane. Voice transcription is lossy, so
those charters arrived with invented Quest numbers, invented acceptance
criteria, and no view of the live board — the Steward then had to reconcile
numbering and state by hand, and pickup spent its days rejecting or repairing
paperwork the lane should never have carried.

The lane is now **requests in, charters out**:
  * a requester (voice agent, remote human, M'Lord away from the desk) lands
    `.court/requests/<id>.json` — intent, not a contract;
  * `court pickup` validates and merges it onto `castle` like any other
    paperwork;
  * the Night's Watch summons ONE bounded Steward, whose job is to read the
    board, allocate the next free Quest id, and author the charter
    (`court request --mark-chartered <id> --quest <Q-id>` closes the request).

A request carries no Quest id. Numbering stays with the agent that can see the
board, which is the whole point.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REQUESTS_DIR = Path(".court") / "requests"
REQUEST_FILE_RE = re.compile(r"^\.court/requests/[^/]+\.json$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")

MAX_TITLE = 160
MIN_ASK = 12


class RequestError(ValueError):
    """A request packet that cannot be used as written."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(text: str, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].strip("-") or "request"


def parse_request(text: str) -> dict[str, Any]:
    """Parse and validate a request packet. Raises ``RequestError``."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RequestError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RequestError("packet must be a JSON object")

    req_id = str(data.get("id") or "").strip()
    if not _ID_RE.match(req_id):
        raise RequestError("id must be a 3-80 char slug of letters/digits/._-")

    title = str(data.get("title") or "").strip()
    if len(title) < 3:
        raise RequestError("title must be at least 3 characters")
    if len(title) > MAX_TITLE:
        raise RequestError(f"title must be at most {MAX_TITLE} characters")

    ask = str(data.get("ask") or "").strip()
    if len(ask) < MIN_ASK:
        raise RequestError(f"ask must be at least {MIN_ASK} characters")

    for key in ("constraints", "links"):
        value = data.get(key)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise RequestError(f"{key} must be a list of strings")

    for key in ("why", "requested_by", "chartered_quest", "created_at"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise RequestError(f"{key} must be a string")

    return {
        "id": req_id,
        "title": title,
        "ask": ask,
        "why": str(data.get("why") or "").strip(),
        "constraints": [s.strip() for s in (data.get("constraints") or []) if s.strip()],
        "links": [s.strip() for s in (data.get("links") or []) if s.strip()],
        "requested_by": str(data.get("requested_by") or "").strip(),
        "created_at": str(data.get("created_at") or _now_iso()),
        "chartered_quest": str(data.get("chartered_quest") or "").strip(),
    }


def validate_request(text: str) -> tuple[bool, str]:
    try:
        parse_request(text)
        return True, ""
    except RequestError as exc:
        return False, str(exc)


def write_request(
    repo: Path,
    *,
    title: str,
    ask: str,
    why: str = "",
    constraints: Optional[list[str]] = None,
    links: Optional[list[str]] = None,
    requested_by: str = "",
    req_id: str = "",
) -> Path:
    """Author a request packet under ``.court/requests/`` (does not commit)."""
    rid = (req_id or f"req-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-{_slug(title, 48)}").strip()
    payload = {
        "id": rid,
        "title": title.strip(),
        "ask": ask.strip(),
        "why": why.strip(),
        "constraints": [c.strip() for c in (constraints or []) if c.strip()],
        "links": [l.strip() for l in (links or []) if l.strip()],
        "requested_by": requested_by.strip(),
        "created_at": _now_iso(),
    }
    parse_request(json.dumps(payload))  # validate before writing
    directory = Path(repo) / REQUESTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{rid}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def load_open(repo: Path) -> list[dict[str, Any]]:
    """Every request on the trunk that has not been chartered yet."""
    directory = Path(repo) / REQUESTS_DIR
    if not directory.is_dir():
        return []
    open_requests: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            packet = parse_request(path.read_text(encoding="utf-8"))
        except (OSError, RequestError):
            continue
        if packet.get("chartered_quest"):
            continue
        packet["path"] = str(path)
        open_requests.append(packet)
    return open_requests


def mark_chartered(repo: Path, req_id: str, quest_id: str) -> Path:
    """Close a request by naming the Quest the Steward chartered for it."""
    path = Path(repo) / REQUESTS_DIR / f"{req_id}.json"
    packet = parse_request(path.read_text(encoding="utf-8"))
    packet["chartered_quest"] = quest_id.strip()
    path.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    return path


def render_brief(packet: dict[str, Any]) -> str:
    """The request as the Steward reads it."""
    lines = [
        f"Request: {packet['id']}",
        f"Title: {packet['title']}",
    ]
    if packet.get("requested_by"):
        lines.append(f"Requested by: {packet['requested_by']}")
    lines.append("")
    lines.append("Ask:")
    lines.append(packet["ask"])
    if packet.get("why"):
        lines.append("")
        lines.append("Why:")
        lines.append(packet["why"])
    if packet.get("constraints"):
        lines.append("")
        lines.append("Constraints:")
        lines.extend(f"- {c}" for c in packet["constraints"])
    if packet.get("links"):
        lines.append("")
        lines.append("Links:")
        lines.extend(f"- {l}" for l in packet["links"])
    return "\n".join(lines)
