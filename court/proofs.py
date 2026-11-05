"""Machine-generated proof for Expected Tribute items.

The failure class this module exists for: in the sibling repository pb-app,
12 of 25 PUNISHED Quests were punished for fabricated, false, or nonexistent
evidence. Q917 shipped correct code but its Tribute claimed commit hashes that
did not exist, file paths that were never created, a test runner the project
does not use, and a "100% match rate" that no command ever produced. An audit
reading agent prose cannot tell an honest claim from a confident fabrication.

So a Tribute claim can now be *backed* by an engine-observed command result.
`record` runs the command itself — through `subprocess`, in the Quest's
worktree when one exists — and stores the exit code, a digest of the output,
the tail of that output, and the git state (HEAD, branch, dirty tree) the
command ran against. `coverage` and `verify` then answer, deterministically
and without a model: which checked Expected Tribute items have a passing
proof recorded at the *current* HEAD, which have only stale proofs from an
older commit, and which have none at all.

Design limits, stated honestly: a proof is only as good as the command the
author asked for. `court prove --item 2 -- pytest tests/test_x.py` proves
that this command exited 0 at this HEAD; it does not prove the command tests
the right thing. The gate is a floor against fabricated evidence, not a
substitute for reading the diff. Proofs are per-checkout durable state under
``.court/proofs/`` and are deliberately not committed by this module (the
caller decides); they are keyed by the Quest charter's file stem so a short
id resolves exactly like the rest of the codebase.

Matching precedence for a record's ``item`` against a checklist item:

  1. it equals the item's 1-based index as a string (``"2"``);
  2. it is a case-insensitive prefix of the item text and at least 8
     characters long (``"Tests pass for"``);
  3. it equals the item text exactly (trailing whitespace ignored).

Index first because it is unambiguous; the 8-character floor stops a two-word
prefix from claiming every item that happens to start with the same word; an
exact full-text match is the most explicit form an author can write.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import store, ward

VERSION = 1
PROOF_DIR = ".court/proofs"
MIN_PREFIX_CHARS = 8
OUTPUT_TAIL_CHARS = 2000
_TIMEOUT_EXIT_CODE = 124


class ProofError(ValueError):
    """A proof operation that cannot be carried out (unresolved Quest, no item)."""


# ---------------------------------------------------------------------------
# Quest resolution
# ---------------------------------------------------------------------------


def _court_root(repo_root: Optional[Path | str]) -> Path:
    if repo_root is None:
        return store.get_court_root()
    root = Path(repo_root)
    return root if root.name == ".court" else root / ".court"


def _as_quest(quest: Any, repo_root: Optional[Path | str] = None) -> Any:
    if isinstance(quest, str):
        return store.load(quest, _court_root(repo_root))
    return quest


def _resolve_stem(quest_id: str, court_root: Path) -> Optional[str]:
    """Map an id (full ``Q917-...`` or unambiguous short ``Q917``) to its stem."""
    try:
        p = store.find_path(quest_id, court_root)
    except OSError:
        return None
    return p.stem if p is not None else None


def proof_path(repo_root: Optional[Path | str], quest_id: str) -> Path:
    """``<repo_root>/.court/proofs/<quest-file-stem>.json``.

    A short id resolves through ``store.find_path``; an id that matches no
    charter, or matches several ambiguously, is an error rather than a guess
    (a proof filed under the wrong Quest is worse than no proof).
    """
    court_root = _court_root(repo_root)
    stem = _resolve_stem(quest_id, court_root)
    if stem is None:
        raise ProofError(
            f"cannot resolve quest {quest_id!r} to a charter file; "
            f"expected one at {court_root}/quests/{quest_id}.md"
        )
    return court_root / "proofs" / f"{stem}.json"


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def _empty(quest_id: str) -> dict:
    return {"version": VERSION, "quest_id": quest_id, "records": []}


def load(repo_root: Optional[Path | str], quest_id: str) -> dict:
    """Return the stored proof envelope; a missing or corrupt file is empty.

    Never raises on file problems — an unreadable proof file must degrade to
    "no proof recorded" so the gate reports the item as unproven instead of
    crashing an audit.
    """
    empty = _empty(quest_id)
    try:
        path = proof_path(repo_root, quest_id)
    except ProofError:
        return empty
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        return empty
    records = [r for r in payload["records"] if isinstance(r, dict)]
    return {
        "version": payload.get("version", VERSION),
        "quest_id": payload.get("quest_id", quest_id),
        "records": records,
    }


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _git_out(cwd: Path, *args: str) -> str:
    try:
        res = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def _git_state(cwd: Path) -> dict:
    if not cwd.is_dir():
        return {"head_sha": "", "branch": "", "tree_dirty": False}
    head = _git_out(cwd, "rev-parse", "HEAD")
    branch = _git_out(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    dirty = bool(_git_out(cwd, "status", "--porcelain"))
    return {"head_sha": head, "branch": branch, "tree_dirty": dirty}


def _digest(stdout: str, stderr: str) -> str:
    h = hashlib.sha256()
    h.update((stdout or "").encode("utf-8", "replace"))
    h.update(b"\n--stderr--\n")
    h.update((stderr or "").encode("utf-8", "replace"))
    return h.hexdigest()


def _run_cwd(quest: Any, repo_root: Path) -> Path:
    from . import git_ops  # local import keeps this module importable without git_ops

    try:
        wt = git_ops.find_worktree_for_quest(quest, repo_root)
    except Exception:
        wt = None
    if wt is not None and Path(wt).is_dir():
        return Path(wt)
    declared = getattr(quest, "worktree", "") or ""
    if declared and Path(declared).is_dir():
        return Path(declared)
    return repo_root


def record(
    quest: Any,
    item: str,
    command: str,
    *,
    repo_root: Optional[Path | str] = None,
    cwd: Optional[Path | str] = None,
    timeout: int = 1800,
    env: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Run ``command`` and append one proof record for ``item``.

    The run directory is the Quest's worktree when one exists as a directory,
    otherwise the Court repository root. ``env`` is merged over the inherited
    environment so a partial pin (``{"DJANGO_SETTINGS_MODULE": ...}``) does
    not erase ``PATH``. A command that exceeds ``timeout`` is recorded as a
    failure with exit code 124, not raised: the gate cares that it did not
    pass, not why.
    """
    quest_obj = _as_quest(quest, repo_root)
    root = Path(repo_root) if repo_root is not None else store.get_court_root().parent
    run_cwd = Path(cwd) if cwd is not None else _run_cwd(quest_obj, root)
    if not run_cwd.is_dir():
        run_cwd = root

    state = _git_state(run_cwd)
    started = now if now is not None else datetime.now(timezone.utc)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    run_env = None if env is None else {**os.environ, **env}

    t0 = datetime.now(timezone.utc)
    timed_out = False
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(run_cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=run_env,
        )
        exit_code = proc.returncode
        stdout, stderr = proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = _TIMEOUT_EXIT_CODE
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        stderr = f"{stderr}\n[proof] command exceeded timeout of {timeout}s".strip()
    except OSError as exc:
        timed_out = False
        exit_code = 127
        stdout, stderr = "", f"[proof] cannot execute command: {exc}"

    finished = datetime.now(timezone.utc)
    seconds = round((finished - t0).total_seconds(), 3)
    combined = stdout + (("\n" + stderr) if stderr else "")

    entry = {
        "item": str(item),
        "command": command,
        "cwd": str(run_cwd),
        "exit_code": exit_code,
        "ok": exit_code == 0 and not timed_out,
        "output_sha256": _digest(stdout, stderr),
        "output_tail": combined[-OUTPUT_TAIL_CHARS:],
        "head_sha": state["head_sha"],
        "branch": state["branch"],
        "tree_dirty": state["tree_dirty"],
        "started": _iso(started),
        "finished": _iso(finished),
        "seconds": seconds,
    }

    path = proof_path(root, getattr(quest_obj, "id", str(quest_obj)))
    payload = load(root, getattr(quest_obj, "id", str(quest_obj)))
    payload.setdefault("version", VERSION)
    payload["quest_id"] = getattr(quest_obj, "id", str(quest_obj))
    payload["records"].append(entry)
    _write_atomic(path, payload)
    return entry


# ---------------------------------------------------------------------------
# Freshness and matching
# ---------------------------------------------------------------------------


def _current_head(cwd: Path, cache: dict) -> Optional[str]:
    key = str(cwd)
    if key not in cache:
        cache[key] = _git_out(cwd, "rev-parse", "HEAD") if cwd.is_dir() else ""
    return cache[key] or None


def _is_stale(entry: dict, cache: dict) -> bool:
    """A proof is stale when its recorded cwd still exists at a different HEAD."""
    raw = entry.get("cwd") or ""
    if not raw:
        return False
    cwd = Path(raw)
    if not cwd.is_dir():
        return False
    head = _current_head(cwd, cache)
    if head is None:
        return False
    return bool(entry.get("head_sha")) and entry["head_sha"] != head


def records_for(
    repo_root: Optional[Path | str],
    quest_id: str,
    item: Optional[str] = None,
    fresh_only: bool = True,
) -> list[dict]:
    """Stored records for a Quest, optionally narrowed to one item and to HEAD.

    ``fresh_only`` drops records whose recorded worktree still exists but now
    sits at a different commit — a proof from an older HEAD proves nothing
    about the tree being audited. A record whose worktree is gone is kept
    (there is no newer HEAD to contradict it).
    """
    records = load(repo_root, quest_id)["records"]
    if item is not None:
        probe = str(item).strip().lower()
        records = [
            r for r in records
            if str(r.get("item", "")).strip().lower() == probe
        ]
    if not fresh_only:
        return records
    cache: dict = {}
    return [r for r in records if not _is_stale(r, cache)]


# ---------------------------------------------------------------------------
# Expected Tribute parsing
# ---------------------------------------------------------------------------


def _item_text(line: str) -> str:
    text = line.strip()
    for marker in ("- [", "* ["):
        if text.startswith(marker):
            text = text[len(marker):]
            break
    if text.startswith(("x]", "X]", " ]", "~]", "-]")):
        text = text[2:]
    elif text and text[0] == "]":
        text = text[1:]
    return text.strip()


def expected_items(quest: Any) -> list[dict]:
    """Parse the ``## Expected Tribute`` checklist, preserving order and state.

    Uses the shared body-section map (``quest.body_sections``) and the same
    ``- [ ]`` / ``- [x]`` / ``- [-]`` markers the Ward already recognises, so
    this module cannot drift from the rest of the Court's checklist parsing.
    """
    quest_obj = _as_quest(quest)
    section = (quest_obj.body_sections or {}).get("Expected Tribute", "")
    items: list[dict] = []
    for raw in (section or "").splitlines():
        line = raw.strip()
        if line.startswith("#") or not line:
            continue
        if line[0] not in "-*" or "[" not in line:
            continue
        checked = bool(ward._CHECKED_RE.match(line))
        unchecked = bool(ward._UNCHECKED_RE.match(line))
        cancelled = bool(ward._CANCELLED_RE.match(line))
        if not (checked or unchecked or cancelled):
            continue
        items.append({
            "index": len(items) + 1,
            "text": _item_text(line),
            "checked": checked,
            "cancelled": cancelled,
        })
    return items


def _matches(entry_item: Any, index: int, text: str) -> bool:
    probe = str(entry_item or "").strip()
    if not probe:
        return False
    if probe == str(index):
        return True
    if len(probe) >= MIN_PREFIX_CHARS and text.lower().startswith(probe.lower()):
        return True
    return probe == text.strip()


def _matches_item(entry: dict, item: dict) -> bool:
    return _matches(entry.get("item"), item["index"], item["text"])


# ---------------------------------------------------------------------------
# Coverage and verification
# ---------------------------------------------------------------------------


def coverage(quest: Any, repo_root: Optional[Path | str] = None) -> dict:
    """Per-item proof coverage for a Quest's Expected Tribute checklist.

    Each item reports ``proofs`` (fresh passing-or-failing records matched to
    it), ``proven`` (at least one fresh record with ``ok is True``), and the
    last matched record's exit code and command for the report. ``ok`` is True
    only when every *checked* item is proven; unchecked items are never
    required and cancelled (``[-]``) items are never required either.
    """
    quest_obj = _as_quest(quest, repo_root)
    quest_id = quest_obj.id
    fresh = records_for(repo_root, quest_id, fresh_only=True)

    items_out: list[dict] = []
    checked_unproven: list[dict] = []
    unchecked: list[dict] = []
    for item in expected_items(quest_obj):
        matched = [r for r in fresh if _matches_item(r, item)]
        proven = any(r.get("ok") is True for r in matched)
        last = matched[-1] if matched else None
        entry = {
            "index": item["index"],
            "text": item["text"],
            "checked": item["checked"],
            "cancelled": item["cancelled"],
            "proofs": len(matched),
            "proven": proven,
            "last_exit_code": last.get("exit_code") if last else None,
            "last_command": last.get("command") if last else None,
        }
        items_out.append(entry)
        required = item["checked"] and not item["cancelled"]
        if required and not proven:
            checked_unproven.append({"index": item["index"], "text": item["text"]})
        if not item["checked"] and not item["cancelled"]:
            unchecked.append({"index": item["index"], "text": item["text"]})

    return {
        "quest_id": quest_id,
        "items": items_out,
        "checked_unproven": checked_unproven,
        "unchecked": unchecked,
        "ok": not checked_unproven,
    }


def verify(quest: Any, repo_root: Optional[Path | str] = None) -> dict:
    """Machine gate: every checked item must have a passing proof at HEAD.

    Unchecked items are not required. A checked item whose only evidence is a
    stale proof (recorded at a different HEAD than its worktree now holds) is
    reported separately from one with no proof at all, because the two need
    different fixes: re-run the command versus write one.
    """
    quest_obj = _as_quest(quest, repo_root)
    quest_id = quest_obj.id
    items = expected_items(quest_obj)
    if items and not any(i["checked"] or i["cancelled"] for i in items):
        # A wholly unticked checklist is vacuously "covered" by the per-item
        # rule above, which is exactly how a FORCED advance with six unchecked
        # items produced an honest-looking but unproven Tribute. Nothing was
        # delivered, so nothing can be proven: fail loudly.
        return {
            "quest_id": quest_id,
            "ok": False,
            "reason": (
                "no Expected Tribute item is checked or cancelled — a wholly "
                "unticked checklist proves nothing; tick the delivered items or "
                "mark genuinely cancelled work as [-]"
            ),
            "checked_unproven": [],
            "stale": [],
            "missing": [],
            "vacuous": True,
        }
    report = coverage(quest_obj, repo_root)
    all_records = records_for(repo_root, quest_id, fresh_only=False)
    fresh_records = records_for(repo_root, quest_id, fresh_only=True)
    cache: dict = {}

    stale: list[dict] = []
    missing: list[dict] = []
    for item in expected_items(quest_obj):
        required = item["checked"] and not item["cancelled"]
        if not required:
            continue
        fresh_matched = [r for r in fresh_records if _matches_item(r, item)]
        if any(r.get("ok") is True for r in fresh_matched):
            continue
        stale_matched = [r for r in all_records if _matches_item(r, item) and _is_stale(r, cache)]
        all_matched = [r for r in all_records if _matches_item(r, item)]
        descriptor = {"index": item["index"], "text": item["text"]}
        if stale_matched:
            stale.append(descriptor)
        elif not all_matched:
            missing.append(descriptor)

    ok = report["ok"]
    if ok:
        reason = ""
    else:
        parts = []
        if report["checked_unproven"]:
            parts.append(
                "checked Expected Tribute item(s) without a passing proof at HEAD: "
                + ", ".join(str(i["index"]) for i in report["checked_unproven"])
            )
        if stale:
            parts.append("stale proof(s) from an older HEAD: " + ", ".join(str(i["index"]) for i in stale))
        if missing:
            parts.append("no proof recorded for item(s): " + ", ".join(str(i["index"]) for i in missing))
        reason = "; ".join(parts) or "unproven checked item(s)"

    return {
        "quest_id": quest_id,
        "ok": ok,
        "reason": reason,
        "checked_unproven": report["checked_unproven"],
        "stale": stale,
        "missing": missing,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _clip(text: str, width: int = 64) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 3] + "..."


def format_report(result: dict) -> str:
    """One plain-text line per checklist item, plus a summary line."""
    lines = [f"Proof coverage for {result.get('quest_id', '?')}"]
    for item in result.get("items", []):
        if item.get("cancelled"):
            marker = "[-]"
        elif item.get("proven"):
            marker = "[x]"
        elif item.get("checked"):
            marker = "[!]"
        else:
            marker = "[ ]"
        exit_code = item.get("last_exit_code")
        command = item.get("last_command")
        detail = f"proofs={item.get('proofs', 0)}"
        if exit_code is not None:
            detail += f" last_exit={exit_code}"
        if command:
            detail += f" last_cmd={_clip(command, 48)}"
        lines.append(f"{marker} {item.get('index')}. {_clip(item.get('text', ''))} | {detail}")
    if result.get("ok"):
        lines.append("RESULT: ok — every checked item has a passing proof at HEAD")
    else:
        unproven = result.get("checked_unproven") or []
        stale = result.get("stale") or []
        missing = result.get("missing") or []
        lines.append(f"RESULT: FAIL — {len(unproven)} checked item(s) lack a passing proof at HEAD")
        for item in unproven:
            lines.append(f"  unproven: {item.get('index')}. {_clip(item.get('text', ''))}")
        for item in stale:
            lines.append(f"  stale-only: {item.get('index')}. {_clip(item.get('text', ''))}")
        for item in missing:
            lines.append(f"  no-proof: {item.get('index')}. {_clip(item.get('text', ''))}")
    return "\n".join(lines)
