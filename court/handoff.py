"""Cheap cold-start handoff helpers.

Two facts drive this module:

1. A worker session's true context usage is the `tokens.total` on its last
   `step-finish` part in kilo.db. Continuing a bloated session re-reads the
   whole history every turn; a fresh session that reads the charter and the
   task file is far cheaper and often sharper.
2. A fresh session needs a *short, directive* prompt — the unchecked
   checklist, the worktree, and explicit context discipline — not a dump of
   the old transcript.

Everything here is deterministic and bounded: kilo.db is read-only and
message-scoped, checklist extraction never returns more than `limit` items,
and no string in this module is allowed to grow with the size of the repo.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from typing import Optional

KILO_DB = os.path.expanduser("~/.local/share/kilo/kilo.db")
DEFAULT_ITEM_WIDTH = 120
DEFAULT_MAX_ITEMS = 12


def session_context_tokens(sid: Optional[str]) -> Optional[int]:
    """True context usage for a session, or None when unknown.

    Mirrors court.ui_server._session_meta's bounded query: the newest 30
    messages' parts only — never a full-session json_extract over a
    multi-GB database."""
    if not sid or not re.fullmatch(r"[\w-]+", sid):
        return None
    if not os.path.exists(KILO_DB):
        return None
    try:
        db = sqlite3.connect(f"file:{KILO_DB}?mode=ro", uri=True, timeout=3)
        db.execute("pragma query_only=1")
        try:
            mids = [r[0] for r in db.execute(
                "select id from message where session_id=?"
                " order by time_created desc limit 30", (sid,))]
            if not mids:
                return None
            q = ",".join("?" * len(mids))
            for (pdata,) in db.execute(
                    f"select data from part where message_id in ({q})"
                    " order by time_created desc", mids):
                try:
                    d = json.loads(pdata)
                except Exception:
                    continue
                if d.get("type") == "step-finish":
                    return int((d.get("tokens") or {}).get("total") or 0)
        finally:
            db.close()
    except Exception:
        return None
    return None


def is_bloated(sid: Optional[str], limit: int) -> tuple[bool, Optional[int]]:
    """(bloated, tokens). A limit of 0 disables the check entirely."""
    if not limit or limit <= 0:
        return False, None
    tokens = session_context_tokens(sid)
    if tokens is None:
        return False, None
    return tokens >= limit, tokens


def worktree_is_clean(worktree) -> bool:
    """True when the worktree has no uncommitted changes to TRACKED files.

    A clean tree is the checkpoint signal for a cull: a fresh session that
    re-reads ``git status`` loses nothing, because everything is on the branch.
    Untracked files are deliberately ignored — every worktree carries
    engine-managed untracked scaffolding (``.kilo/court``, ``.kilo/kilo.json``)
    that would otherwise make the tree permanently "dirty", and genuine untracked
    work still lives on disk, so a fresh session sees it. Unknown (not a repo,
    git missing, any error) reports as NOT clean — conservatively keeping the
    existing session rather than cold-starting over in-flight edits."""
    if not worktree:
        return False
    try:
        res = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if res.returncode != 0:
        return False
    return not res.stdout.strip()


def continuation_decision(
    tokens: Optional[int],
    cull_soft: int = 0,
    hard_red_zone: int = 0,
    checkpointed: bool = False,
) -> tuple[bool, str]:
    """Decide whether to COLD-START a fresh session instead of continuing one.

    Two thresholds, in order of force:

    * ``hard_red_zone`` — never re-read this much: always cold-start.
    * ``cull_soft``     — a cull *opportunity*, taken only when the worktree is
                          checkpointed (committed). Uncommitted work keeps the
                          session so a fresh start cannot lose it.

    Returns ``(cold_start, reason)``. A threshold of 0 disables its rule; an
    unknown token count never triggers a cold start."""
    if tokens is None:
        return False, "context unknown — continuing the recorded session"
    if hard_red_zone and tokens >= hard_red_zone:
        return True, f"red zone: ~{tokens:,} tokens >= hard cap {hard_red_zone:,}"
    if cull_soft and tokens >= cull_soft:
        if checkpointed:
            return True, f"cull opportunity: ~{tokens:,} tokens >= soft cap {cull_soft:,} with a committed tree"
        return False, f"~{tokens:,} tokens >= soft cap {cull_soft:,} but uncommitted work — continuing to avoid loss"
    return False, f"under the soft cap (~{tokens:,} tokens)"


def unchecked_items(quest, limit: int = DEFAULT_MAX_ITEMS, width: int = DEFAULT_ITEM_WIDTH) -> list[str]:
    """Unchecked `- [ ]` lines from a quest's Expected Tribute.

    Deterministic, bounded, and safe to paste into a fresh session's prompt:
    finished work is not restated, and each item is clipped to `width`."""
    body = ""
    try:
        body = (quest.body_sections or {}).get("Expected Tribute", "") or ""
    except Exception:
        return []
    out: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s.startswith("- [ ]"):
            continue
        text = re.sub(r"^-\s*\[\s*\]\s*", "", s).strip()
        if not text:
            continue
        out.append(text[:width] + ("…" if len(text) > width else ""))
        if len(out) >= limit:
            break
    return out


def cold_start_prompt(quests: list, prior_tokens: Optional[int] = None, task_file: str = "TASK.md") -> str:
    """A compact, directive prompt for a FRESH session picking up mid-task.

    Deliberately short: the fresh session reads the charter and the worktree
    itself. The point of the cold start is to NOT reload a bloated history, so
    this prompt must never become a transcript summary."""
    ids = ", ".join(getattr(q, "id", "?") for q in quests) or "(unknown quest)"
    lines = [
        f"Resume Quest {ids} in this worktree. The previous session ended mid-task; "
        f"this is a fresh session, so trust the files on disk, not any memory of earlier turns.",
        "",
        f"1. Read the worktree charter: `.kilo/{task_file}` if present, otherwise the quest file "
        f"under `.court/quests/`. Read only its `Expected Tribute` checklist and `Castle Ledger` — "
        f"do not read the whole record.",
        "2. Run `git status --short` and `git diff --stat` to see what already landed. Do not redo "
        "committed work.",
        "3. Finish the unchecked items below, then run the verification the charter names, write the "
        "role paperwork, and advance the quest yourself.",
        "",
        "Context discipline (this keeps you fast and cheap):",
        "- Never dump a whole file, log, artifact, or dataset into context. Use `grep -n`, `sed -n 'A,Bp'`, "
        "or `head`/`tail` on the smallest window that answers the question.",
        "- Do not re-run steps whose output is already on disk; read the existing artifact's summary first.",
        "- Keep command output short (`--stat`, `--quiet`, `-q`, `| tail -20`).",
        "",
    ]
    if prior_tokens:
        lines.insert(1, f"(Previous session context was ~{prior_tokens:,} tokens; that is why this is a fresh session.)")
    for q in quests:
        items = unchecked_items(q)
        if not items:
            continue
        lines.append(f"Unchecked items for {getattr(q, 'id', '?')}:")
        lines.extend(f"- {it}" for it in items)
        lines.append("")
    lines.append("Do not end your turn with the task unfinished.")
    return "\n".join(lines)
