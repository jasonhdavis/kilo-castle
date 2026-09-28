"""Studio close-out lifecycle (`court studio <ids> --close`).

Owns the entire back half of the combined-artist-studio lifecycle behind five
guards, a proof manifest, and a gated teardown — the deterministic close-out
that standup (`court studio <ids>`) is the front half of.

Guards (each maps to a real failure the studio lifecycle produced in practice):

1. Sign-off proof    — close refuses unless every cohort Quest carries a dated
                       ``studio sign-off`` ledger stamp (or ``--signoff`` writes
                       one at invocation). Sign-off used to live only in commit
                       messages and brief prose; nothing machine-checkable
                       guarded teardown.
2. Base drift        — measures merge-base(studio_branch, castle) distance; a
                       drifted base replayed ~19k stale lines into 5 Quest
                       branches when a whole-branch sync-back merged against a
                       ~2000-commit-old cut point. Beyond the threshold the
                       close refuses and recommends a re-cut (``--force-union``
                       overrides).
3. Convoy race       — refuses a Quest already stamped into a live convoy or
                       merged onto an active ``the-gatehouse/*`` branch (a
                       Gatekeeper merged tainted branches mid-race once; the
                       close must never race an in-flight convoy).
4. Cherry-pick       — extraction of the artist's labeled review commits
   extraction          (``style(...): <QID> ... royal review ...`` subject
                       and/or ``Addendum-Quests:`` trailer) instead of
                       whole-branch merge; clean commits are cherry-picked with
                       ``-x`` provenance, conflicts abort cleanly (never
                       auto-union), and already-applied commits are verified
                       no-ops. Whole-branch merge hid the case where a Quest's
                       entire approved chain existed only on the studio branch.
5. Union brief        — conflicted Quests route to the artist with a
   generator           machine-generated brief (conflict regions + the
                       branch-side features the approved design predates) and a
                       ``Studio Close: UNION-PENDING`` ledger marking; the
                       close path never union-resolves.

The close-out manifest (``.court/studio-close/<slug>/manifest.md``) records the
per-Quest outcome — synced (with provenance hashes), union-pending (brief
path), already-present (verified no-op), or race-blocked — plus a per-Quest
"approved UI present on branch" check. Teardown (runserver kill -> studio
worktree to the Ashes section while its session still lives -> session stop;
branch ref kept) is unlocked only when the manifest is all-green or an explicit
override is passed.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import config, git_ops

SIGNOFF_MARKER = "studio sign-off"
UNION_PENDING_MARKER = "Studio Close: UNION-PENDING"

# Artist polish-commit label: `style(x): Q6xx royal review rev N` (case
# vary). The subject must name the Quest id segment AND say "royal review".
_LABELED_SUBJECT = re.compile(r"^style\(.*\):\s*.*\b(?P<qid>q\d+)\b.*royal review", re.IGNORECASE)
_ADDENDUM_TRAILER = re.compile(r"^Addendum-Quests:\s*(?P<list>.+)$", re.MULTILINE | re.IGNORECASE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(cmd: list[str], cwd: Optional[Path] = None, timeout: int = 120) -> dict:
    return git_ops._run(cmd, cwd or Path.cwd(), timeout=timeout)


def _git_out(cmd: list[str], cwd: Optional[Path] = None, timeout: int = 120) -> str:
    res = _git(cmd, cwd=cwd, timeout=timeout)
    return (res.get("stdout") or "") if res.get("ok") else ""


def quest_tokens(quest_id: str) -> set[str]:
    """Id tokens a polish commit may name the Quest by: the full id and its
    leading short segment (``Q617-Shops-X`` also answers to ``Q617``)."""
    toks = set()
    if quest_id:
        toks.add(quest_id.lower())
        toks.add(quest_id.split("-", 1)[0].lower())
    return toks


def commit_names_quest(subject: str, body: str, quest_id: str) -> bool:
    """Does one studio-branch commit carry this Quest's artist-review label?"""
    toks = quest_tokens(quest_id)
    if not toks:
        return False
    m = _LABELED_SUBJECT.match(subject or "")
    if m and m.group("qid").lower() in toks:
        return True
    for trailer in _ADDENDUM_TRAILER.finditer(body or ""):
        listed = {t.strip().lower() for t in trailer.group("list").split(",")}
        if listed & toks:
            return True
    return False


# ---------------------------------------------------------------------------
# Guard 1 — sign-off proof
# ---------------------------------------------------------------------------

def signoff_line(quest) -> Optional[str]:
    """The dated ``studio sign-off`` ledger stamp on this Quest, if any.
    Ledger entries are ``- **<ts>** — <text>`` bullets under Castle Ledger;
    the marker must appear in a dated bullet to count as proof."""
    section = (quest.body_sections.get("Castle Ledger") or "")
    for line in section.splitlines():
        if SIGNOFF_MARKER.lower() in line.lower() and re.search(r"\*\*\d{4}-\d{2}-\d{2}", line):
            return line.strip()
    return None


def apply_signoff(quest, note: str) -> None:
    quest.log_ledger(
        quest.status, quest.status,
        f"{SIGNOFF_MARKER}: {note} (dated {_now_iso()}, written at close invocation)",
    )


# ---------------------------------------------------------------------------
# Guard 2 — base drift
# ---------------------------------------------------------------------------

def base_drift_count(repo_root: Path, studio_branch: str, base_branch: str = "castle") -> Optional[int]:
    """Commits the base branch advanced since the studio branch was cut
    (``rev-list --count $(merge-base studio base)..base``). None on any git
    failure so callers treat it as unmeasurable and refuse."""
    mb = _git_out(["git", "merge-base", studio_branch, base_branch], repo_root).strip()
    if not mb:
        return None
    out = _git_out(["git", "rev-list", "--count", f"{mb}..{base_branch}"], repo_root).strip()
    return int(out) if out.isdigit() else None


def drift_threshold(args, court_dir: Optional[Path] = None) -> int:
    """Threshold precedence: --drift-threshold > studio.close_max_base_drift > 100.
    A configured 0 is legitimate (refuse any drift) — never collapse it via
    falsy-or-default."""
    explicit = getattr(args, "drift_threshold", None)
    if explicit is not None:
        return max(0, int(explicit))
    cfg = config.load_config(court_dir)
    studio = cfg.get("studio") if isinstance(cfg.get("studio"), dict) else {}
    raw = studio.get("close_max_base_drift")
    if raw is None:
        return 100
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 100


# ---------------------------------------------------------------------------
# Guard 3 — convoy race
# ---------------------------------------------------------------------------

def gatehouse_race(repo_root: Path, quest) -> Optional[str]:
    """Blocking integration lane for this Quest, if any: a stamped cogship
    whose gatehouse branch still exists, or a ``the-gatehouse/*`` branch that
    already contains the Quest branch tip."""
    from . import store as court_store

    stamped = court_store.normalize_cogship_id(getattr(quest, "cogship_id", "") or "")
    if stamped:
        gh = f"the-gatehouse/{stamped}"
        exists = _git_out(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{gh}"], repo_root).strip()
        if exists:
            return f"stamped into live convoy {stamped} (branch {gh} still exists)"
    tip = _git_out(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{quest.branch}"], repo_root).strip()
    if not tip:
        return None
    branches = [
        b.strip().removeprefix("refs/heads/")
        for b in _git_out(["git", "for-each-ref", "--format=%(refname)", "refs/heads/the-gatehouse/"], repo_root).splitlines()
        if b.strip()
    ]
    for gh in branches:
        contained = _git(["git", "merge-base", "--is-ancestor", tip, gh], repo_root)
        if contained.get("ok"):
            return f"branch tip already merged onto active gatehouse branch {gh}"
    return None


# ---------------------------------------------------------------------------
# Guard 4 — labeled-commit extraction
# ---------------------------------------------------------------------------

def labeled_commits(repo_root: Path, studio_branch: str, quest_ids: list[str], base_branch: str = "castle") -> dict[str, list[dict]]:
    """Artist review commits per Quest id, oldest-first.

    Range: merge-base(studio, base)..studio — everything added after the cut
    point. Each commit's subject + body is matched against the Quest's label
    tokens; touched files come from diff-tree for the manifest and the
    approved-UI check.
    """
    mb = _git_out(["git", "merge-base", studio_branch, base_branch], repo_root).strip()
    rng = f"{mb}..{studio_branch}" if mb else studio_branch
    log = _git_out(
        ["git", "log", "--reverse", "--topo-order", "--format=%H%x1f%s%x1f%b%x1e", rng],
        repo_root,
    )
    out: dict[str, list[dict]] = {qid: [] for qid in quest_ids}
    for rec in [r for r in log.split("\x1e") if r.strip()]:
        parts = rec.strip("\n").split("\x1f")
        if len(parts) < 3:
            continue
        sha, subject, body = parts[0], parts[1], parts[2]
        files = [
            ln.strip()
            for ln in _git_out(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", sha], repo_root).splitlines()
            if ln.strip()
        ]
        for qid in quest_ids:
            if commit_names_quest(subject, body, qid):
                out[qid].append({"hash": sha, "subject": subject, "files": files})
    return out


def applied_patch_ids(repo_root: Path, quest_branch: str, studio_branch: str) -> set[str]:
    """Studio-side commits whose patch-id is already applied on the Quest
    branch (``git cherry`` '-' lines)."""
    applied = set()
    for line in _git_out(["git", "cherry", quest_branch, studio_branch], repo_root).splitlines():
        line = line.strip()
        if line.startswith("-") and len(line) > 2:
            applied.add(line[1:].strip())
    return applied


def worktree_state(repo_root: Path, wt_path: Optional[str]) -> dict:
    """Existence + cleanliness of a Quest worktree (extraction requires both)."""
    if not wt_path:
        return {"exists": False, "clean": False, "on_branch": False}
    p = Path(wt_path)
    if not p.is_dir():
        return {"exists": False, "clean": False, "on_branch": False}
    status = _git_out(["git", "status", "--porcelain"], p)
    branch = _git_out(["git", "rev-parse", "--abbrev-ref", "HEAD"], p).strip()
    return {
        "exists": True,
        "clean": not status.strip(),
        "on_branch": bool(branch),
        "dirty": status.strip()[:400],
    }


def cherry_pick_labeled(repo_root: Path, wt_path: str, commits: list[dict]) -> dict:
    """Cherry-pick the labeled commits oldest-first with ``-x`` provenance.
    On any conflict: record the conflicted files, abort, restore — the close
    path never auto-unions and never forces."""
    picked: list[dict] = []
    for c in commits:
        res = _git(["git", "cherry-pick", "-x", c["hash"]], Path(wt_path))
        if res.get("ok"):
            new = _git_out(["git", "rev-parse", "HEAD"], Path(wt_path)).strip()
            picked.append({"source": c["hash"], "subject": c["subject"], "new_hash": new})
            continue
        conflicts = [
            ln.strip()
            for ln in _git_out(["git", "diff", "--name-only", "--diff-filter=U"], Path(wt_path)).splitlines()
            if ln.strip()
        ]
        conflict_markers = ""
        for f in conflicts[:10]:
            try:
                conflict_markers += f"\n--- {f} ---\n" + (Path(wt_path) / f).read_text(encoding="utf-8", errors="replace")[-4000:]
            except OSError:
                pass
        _git(["git", "cherry-pick", "--abort"], Path(wt_path))
        _git(["git", "reset", "--hard", "HEAD"], Path(wt_path))
        reason = ((res.get("stderr") or "") + (res.get("stdout") or "")).strip().splitlines()
        return {
            "status": "conflict",
            "picked": picked,
            "conflicts": conflicts,
            "conflict_markers": conflict_markers[:6000],
            "reason": reason[-1][:200] if reason else "cherry-pick conflict",
        }
    return {"status": "picked", "picked": picked, "conflicts": [], "conflict_markers": "", "reason": ""}


# ---------------------------------------------------------------------------
# Approved-UI presence check (manifest YES/NO)
# ---------------------------------------------------------------------------

def approved_ui_present(repo_root: Path, quest_branch: str, studio_branch: str, commits: list[dict]) -> tuple[bool, str]:
    """YES iff every labeled artist commit is patch-id-applied on the Quest
    branch, or every file those commits touch is byte-identical between the
    branch tip and the studio tip. Catches the approved-design-exists-only-on-
    the-studio-branch failure class."""
    if not commits:
        return False, "no labeled artist commits found on the studio branch"
    applied = applied_patch_ids(repo_root, quest_branch, studio_branch)
    if all(c["hash"] in applied for c in commits):
        return True, f"all {len(commits)} labeled commit(s) patch-id-applied on {quest_branch}"
    files = sorted({f for c in commits for f in c["files"]})
    for f in files:
        a = _git_out(["git", "show", f"{studio_branch}:{f}"], repo_root)
        b = _git_out(["git", "show", f"{quest_branch}:{f}"], repo_root)
        if a != b:
            return False, f"{f} differs between studio tip and {quest_branch} tip (labeled commit(s) not fully applied)"
    return True, f"{len(files)} labeled file(s) byte-identical between studio tip and {quest_branch}"


# ---------------------------------------------------------------------------
# Guard 5 — union brief
# ---------------------------------------------------------------------------

def _bounded_diff(repo_root: Path, rng: str, paths: list[str], cap: int = 4000) -> str:
    if not paths:
        return "(none)"
    out = _git_out(["git", "diff", "--stat", rng, "--"] + paths, repo_root).strip()
    detail = _git_out(["git", "diff", rng, "--"] + paths, repo_root).strip()
    if len(detail) > cap:
        detail = detail[:cap] + "\n…[truncated — run git diff directly for the full region]"
    return (out + "\n\n" + detail).strip() or "(no changes)"


def write_union_brief(
    repo_root: Path,
    slug: str,
    quest,
    studio_branch: str,
    base_branch: str,
    pick_report: dict,
    studio_tip_files: list[str],
) -> Path:
    """Machine-generated brief for a conflicted Quest: conflict regions plus
    the branch-side features the approved design predates (and vice versa)."""
    q_branch = quest.branch
    mb = _git_out(["git", "merge-base", studio_branch, q_branch], repo_root).strip() or base_branch
    conflict_files = pick_report.get("conflicts") or []
    overlap = sorted(set(conflict_files) | set(studio_tip_files))
    lines = [
        f"# Union Brief — {quest.id} (studio {slug})",
        "",
        f"- Generated: {_now_iso()} (deterministic close-path output; never auto-unioned)",
        f"- Studio branch: `{studio_branch}` · Quest branch: `{q_branch}` · merge-base: `{mb[:12]}`",
        f"- Reason: cherry-pick of labeled artist commit(s) conflicted — {pick_report.get('reason') or 'conflict'}",
        "",
        "## Conflict regions",
        "",
    ]
    if conflict_files:
        lines += [f"- `{f}`" for f in conflict_files]
        if pick_report.get("conflict_markers"):
            lines += ["", "```", pick_report["conflict_markers"].strip(), "```"]
    else:
        lines.append("(no conflict file list captured)")
    lines += [
        "",
        "## Branch-side features the approved design predates",
        "What the Quest branch shipped since the merge-base that the studio tip",
        "does not contain — a blind union would regress exactly these.",
        "",
        "```",
        _bounded_diff(repo_root, f"{mb}..{q_branch}", overlap),
        "```",
        "",
        "## Approved-design changes on the studio branch",
        "What the reviewed studio state changes in the same files — the content",
        "the artist session approved and the branch is missing.",
        "",
        "```",
        _bounded_diff(repo_root, f"{mb}..{studio_branch}", overlap),
        "```",
        "",
        "## Ruling needed",
        "Route to a dedicated artist session in the live studio worktree",
        "(`court studio <ids> --close --artist-session` or the printed spawn",
        "command); the artist re-lands the approved design on top of these",
        "features. The close path never union-resolves this Quest.",
        "",
    ]
    d = repo_root / ".court" / "studio-close" / slug
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{quest.id}-union-brief.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------

def stop_studio_server(repo_root: Path, wt_path: Path) -> str:
    """Kill the studio runserver: manage_servers.sh stop when the project
    supplies the script, else kill the listener on the worktree's port file."""
    script = repo_root / ".kilo" / "manage_servers.sh"
    if script.exists():
        res = subprocess.run(
            ["bash", str(script), "stop", str(wt_path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if res.returncode == 0:
            return "runserver stopped via manage_servers.sh"
        return f"manage_servers.sh stop exited {res.returncode}: {(res.stderr or res.stdout).strip()[:160]}"
    port_file = wt_path / ".worktree-port"
    try:
        port = int(port_file.read_text().strip())
    except (OSError, ValueError):
        return "no .worktree-port — no runserver record to stop"
    pids = _git_out(["lsof", "-ti", f"tcp:{port}"], repo_root).split()
    killed = []
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGTERM)
            killed.append(pid)
        except (OSError, ValueError):
            pass
    return f"stopped listener(s) on port {port}: {','.join(killed) or 'none'}"


def stop_cli_artist_session(wt_path: Path) -> list[int]:
    """Kill any live headless artist turn rooted in the studio worktree."""
    killed: list[int] = []
    try:
        ps = subprocess.run(
            ["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=15, check=False,
        ).stdout
    except Exception:
        return killed
    for line in ps.splitlines():
        if "kilo run" not in line or str(wt_path) not in line or "--agent artist" not in line:
            continue
        try:
            pid = int(line.strip().split(None, 1)[0])
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            killed.append(pid)
        except (OSError, ValueError, ProcessLookupError):
            continue
    for pid in killed:
        time.sleep(0.5)
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    return killed


def teardown_agent_manager_commands(repo_root: Path, wt_path: Path, studio_branch: str, session_id: str) -> list[dict]:
    """The Agent Manager teardown sequence in move-before-stop order, resolved
    against the live agent-manager.json. Court runs as a plain process and
    cannot invoke the Agent Manager tool, so these are returned for the
    Steward/royal session to execute verbatim."""
    from .cli import agent_manager_json_path
    am_path = agent_manager_json_path()
    try:
        am = json.loads(am_path.read_text(encoding="utf-8")) if am_path.exists() else {}
    except (OSError, ValueError):
        am = {}
    ashes = next(
        (sid for sid, sec in am.get("sections", {}).items() if sec.get("name") == "Ashes"),
        None,
    )
    wt_id = next(
        (wid for wid, w in am.get("worktrees", {}).items()
         if w.get("path") == str(wt_path) or w.get("branch") == studio_branch),
        None,
    )
    sess = session_id or next(
        (sid for sid, s in am.get("sessions", {}).items() if wt_id and s.get("worktreeId") == wt_id),
        None,
    )
    cmds = []
    if sess and ashes:
        cmds.append({"op": "agent_manager move", "sessionID": sess, "sectionID": ashes,
                     "why": "move the studio session (with its worktree) to Ashes while it still lives"})
    else:
        cmds.append({"op": "agent_manager move", "sessionID": sess or "<session-id>",
                     "sectionID": ashes or "<ashes-section-id>",
                     "why": "resolve the studio session/Ashes section in the Agent Manager UI"})
    if sess:
        cmds.append({"op": "agent_manager stop", "sessionID": sess,
                     "why": "stop the studio session ONLY after the Ashes move"})
    return cmds


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def render_manifest(slug: str, studio_branch: str, wt_path: Path, base_branch: str, rows: list[dict], meta: dict) -> str:
    out = [
        f"# Studio Close-Out Manifest — {slug}",
        "",
        f"- Generated: {_now_iso()}",
        f"- Studio branch: `{studio_branch}` (worktree: `{wt_path}`) — branch ref kept; worktree teardown is the command's, physical deletion stays manual",
        f"- Base: `{base_branch}` · base drift: {meta.get('drift', 'n/a')} commits (threshold {meta.get('threshold', 'n/a')}){meta.get('drift_note', '')}",
        f"- Sign-off: {meta.get('signoff_note', '')}",
        "",
        "| Quest | Outcome | Provenance / Brief | Approved UI on branch |",
        "|---|---|---|---|",
    ]
    for r in rows:
        prov = r.get("provenance") or "-"
        out.append(f"| {r['id']} | {r['outcome']} | {prov} | {r.get('ui_present') or '-'} |")
    verdict = meta.get("verdict", "BLOCKED")
    out += ["", f"**Verdict: {verdict}**", ""]
    if verdict != "ALL-GREEN":
        out.append("Teardown locked: resolve the non-green rows (or pass --override-manifest) and re-run --close.")
        out.append("")
    for n in meta.get("notes", []):
        out.append(f"- {n}")
    return "\n".join(out) + "\n"


def all_green(rows: list[dict]) -> bool:
    return bool(rows) and all(r["outcome"] in ("synced", "already-present") and r.get("ui_present", "").startswith("YES") for r in rows)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

_HARD_FAILURE_OUTCOMES = {"refused", "race-blocked", "blocked"}


def _row(qid: str, outcome: str, provenance: str = "", ui_present: str = "") -> dict:
    return {"id": qid, "outcome": outcome, "provenance": provenance, "ui_present": ui_present}


def run_close(args) -> int:
    """`court studio <ids> --close`: guards -> extraction + close-out manifest
    -> gated teardown. Returns the process exit code."""
    from .cli import _studio_slug, resolve_quest_selection, standup_kilo_session
    from . import store as court_store

    if getattr(args, "sync_back", False):
        print("ERROR: --close and --sync-back are mutually exclusive — --close supersedes it "
              "(extraction is cherry-pick based, not a whole-branch merge).", file=sys.stderr)
        return 2

    auto_commit = not getattr(args, "no_commit", False)
    as_json = bool(getattr(args, "json", False))
    repo_root = git_ops.get_repo_root()
    quests = resolve_quest_selection(args, batchable=True, required=True)
    base_branch = getattr(args, "base", "castle") or "castle"
    slug = _studio_slug(quests)
    studio_branch = getattr(args, "branch", None) or f"artist/{slug}-ui-studio"

    if not _git_out(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{studio_branch}"], repo_root).strip():
        print(
            f"ERROR: studio branch '{studio_branch}' not found in this repo — nothing to close. "
            "Pass the exact studio branch with --branch if the cohort was cut under a different name.",
            file=sys.stderr,
        )
        return 1
    wt_path = repo_root / ".kilo" / "worktrees" / f"artist-studio-{slug}"
    if not wt_path.is_dir():
        wt_path = None  # worktree may already be gone; extraction still runs via branch refs where possible

    rows: list[dict] = []
    meta_notes: list[str] = []

    # ---- Guard 1: sign-off proof -----------------------------------------
    signoff_note = (getattr(args, "signoff", None) or "").strip()
    missing_signoff = [q.id for q in quests if not signoff_line(q)]
    if missing_signoff and not signoff_note:
        for q in quests:
            if q.id in missing_signoff:
                rows.append(_row(q.id, "refused", "no dated 'studio sign-off' ledger stamp (pass --signoff \"<note>\")"))
            else:
                rows.append(_row(q.id, "refused", "close refused cohort-wide: sign-off missing on " + ", ".join(missing_signoff)))
        meta = {"verdict": "BLOCKED", "threshold": drift_threshold(args, repo_root / ".court" if (repo_root / ".court").is_dir() else None),
                "signoff_note": "missing on: " + ", ".join(missing_signoff)}
        manifest = _persist_manifest(repo_root, slug, studio_branch, wt_path, base_branch, rows, meta, auto_commit)
        _print_close(slug, studio_branch, rows, meta, manifest, None)
        return 1
    if signoff_note:
        changed = []
        for q in quests:
            if not signoff_line(q):
                apply_signoff(q, signoff_note)
                changed.append(q)
        if changed:
            court_store.save_many(changed, f"court: studio close {slug} sign-off stamp", auto_commit=auto_commit)
        meta_notes.append(f"sign-off written at invocation for {len(changed)} quest(s): \"{signoff_note}\"")

    # ---- Guard 2: base drift ---------------------------------------------
    drift = base_drift_count(repo_root, studio_branch, base_branch)
    threshold = drift_threshold(args, repo_root / ".court" if (repo_root / ".court").is_dir() else None)
    force_union = bool(getattr(args, "force_union", False))
    drift_note_txt = ""
    if drift is None:
        for q in quests:
            rows.append(_row(q.id, "refused", "base drift unmeasurable (merge-base failed) — refusing rather than guessing"))
        meta = {"verdict": "BLOCKED", "threshold": threshold, "signoff_note": "ledger marker / --signoff"}
        manifest = _persist_manifest(repo_root, slug, studio_branch, wt_path, base_branch, rows, meta, auto_commit)
        _print_close(slug, studio_branch, rows, meta, manifest, None)
        return 1
    if drift > threshold:
        if not force_union:
            for q in quests:
                rows.append(_row(
                    q.id, "refused",
                    f"base drift {drift} commits exceeds threshold {threshold} — re-cut the studio from the current {base_branch} tip (--force-union overrides)",
                ))
            meta = {"verdict": "BLOCKED", "threshold": threshold, "drift": drift,
                    "drift_note": " — REFUSED: re-cut recommended", "signoff_note": "ledger marker / --signoff"}
            manifest = _persist_manifest(repo_root, slug, studio_branch, wt_path, base_branch, rows, meta, auto_commit)
            _print_close(slug, studio_branch, rows, meta, manifest, None)
            return 1
        drift_note_txt = f"base drift {drift} commits exceeds threshold {threshold} — FORCED via --force-union"
        meta_notes.append(drift_note_txt)
    else:
        drift_note_txt = f"base drift {drift} commits (threshold {threshold})"

    # ---- Guards 3-5 + phase 2 per quest -----------------------------------
    labeled = labeled_commits(repo_root, studio_branch, [q.id for q in quests], base_branch)
    union_quests: list[tuple[Any, Path]] = []
    changed_quests: list[Any] = []

    for q in quests:
        # Guard 3: convoy race
        race = gatehouse_race(repo_root, q)
        if race:
            rows.append(_row(q.id, "race-blocked", race))
            continue

        commits = labeled.get(q.id) or []
        if not commits:
            # Nothing labeled to extract: already-present only when the studio
            # reviewed exactly this branch tip and the artist added nothing
            # (the Q628/Q644 verified-no-op shape).
            contains = _git(["git", "merge-base", "--is-ancestor", q.branch, studio_branch], repo_root)
            if contains.get("ok"):
                rows.append(_row(
                    q.id, "already-present",
                    "no labeled artist commits on the studio branch; studio reviewed this exact branch tip (verified no-op)",
                    "YES",
                ))
            else:
                rows.append(_row(
                    q.id, "blocked",
                    "no labeled artist commits and the studio never reviewed this branch tip — inspect manually",
                ))
            continue

        applied = applied_patch_ids(repo_root, q.branch, studio_branch)
        if all(c["hash"] in applied for c in commits):
            ok, detail = approved_ui_present(repo_root, q.branch, studio_branch, commits)
            rows.append(_row(
                q.id, "already-present",
                f"all {len(commits)} labeled commit(s) patch-id-applied on {q.branch} (verified no-op)",
                "YES" if ok else "NO",
            ))
            if not ok:
                meta_notes.append(f"{q.id}: patch-ids applied but labeled files differ — {detail}")
            continue

        # Guard 4: cherry-pick extraction (requires the quest worktree)
        ws = worktree_state(repo_root, q.worktree)
        if not ws["exists"]:
            rows.append(_row(q.id, "blocked", f"no worktree at '{q.worktree or '-'}' to extract into"))
            continue
        if not ws["clean"]:
            rows.append(_row(q.id, "blocked", f"dirty worktree — commit or stash first ({ws.get('dirty', '')[:120]})"))
            continue

        pick = cherry_pick_labeled(repo_root, q.worktree, commits)
        if pick["status"] == "conflict":
            studio_tip_files = sorted({f for c in commits for f in c["files"]})
            brief = write_union_brief(repo_root, slug, q, studio_branch, base_branch, pick, studio_tip_files)
            q.log_ledger(q.status, q.status, f"{UNION_PENDING_MARKER} (brief: {brief})")
            changed_quests.append(q)
            rows.append(_row(q.id, "union-pending", f"{len(pick['picked'])} picked before conflict; brief: {brief}", "NO"))
            union_quests.append((q, brief))
            continue

        hashes = ", ".join(p["new_hash"][:12] for p in pick["picked"])
        q.log_ledger(
            q.status, q.status,
            f"Studio close: cherry-picked {len(pick['picked'])} labeled artist commit(s) from {studio_branch} "
            f"({hashes}) — provenance via -x; collection next",
        )
        changed_quests.append(q)
        rows.append(_row(q.id, "synced", f"{len(pick['picked'])} commit(s): {hashes}", "YES"))

    if changed_quests:
        court_store.save_many(changed_quests, f"court: studio close {slug} paperwork", auto_commit=auto_commit)

    # ---- Manifest ----------------------------------------------------------
    verdict = "ALL-GREEN" if all_green(rows) else "BLOCKED"
    meta = {
        "verdict": verdict,
        "drift": drift,
        "threshold": threshold,
        "drift_note": f" — {drift_note_txt}" if drift_note_txt else "",
        "signoff_note": signoff_note or "ledger marker",
        "notes": meta_notes,
    }
    manifest = _persist_manifest(repo_root, slug, studio_branch, wt_path, base_branch, rows, meta, auto_commit)

    # ---- Phase 3: gated teardown ------------------------------------------
    override = bool(getattr(args, "override_manifest", False))
    skip_teardown = bool(getattr(args, "skip_teardown", False))
    teardown_info: dict = {}
    if skip_teardown:
        teardown_info["status"] = "skipped (--skip-teardown)"
    elif verdict == "ALL-GREEN" or override:
        teardown_info = _teardown(repo_root, slug, wt_path, studio_branch, quests, as_json)
    else:
        teardown_info["status"] = "locked (manifest not all-green; --override-manifest to force)"

    # ---- Union artist session ---------------------------------------------
    artist_spawn: dict = {}
    if union_quests and getattr(args, "artist_session", False):
        if wt_path and wt_path.is_dir():
            briefs_txt = "\n\n".join(f.read_text(encoding="utf-8") for _, f in union_quests)
            prompt = (
                f"You are the Court Artist resolving UNION-PENDING quests for combined studio {slug}.\n"
                f"Worktree: {wt_path}\nBranch: {studio_branch}\n\n"
                "Re-land the approved design on top of the branch-side features listed in each brief; "
                "never blind-union. Commit per quest with the established "
                "`style(x): <QID> royal review rev N` subject and an `Addendum-Quests:` trailer.\n\n"
                f"{briefs_txt}"
            )
            spawn = standup_kilo_session(
                wt_path, agent="artist",
                model=getattr(args, "model", None) or config.get_model("artist"),
                prompt=prompt, title=f"{slug} union-pending resolution",
                kilo_bin=None, provider_hint="artist",
            )
            artist_spawn = {"message": spawn.get("message", ""), "session_id": spawn.get("session_id", "")}
        else:
            artist_spawn = {"error": "studio worktree gone — spawn the artist session manually with the brief(s)"}
    elif union_quests:
        artist_spawn["hint"] = (
            "union-pending quests route to the artist: re-run with --artist-session, or run "
            f"`kilo run --agent artist --dir {wt_path or '<studio worktree>'} \"<brief>\"`"
        )

    _print_close(slug, studio_branch, rows, meta, manifest, teardown_info, artist_spawn, as_json)
    return 1 if any(r["outcome"] in _HARD_FAILURE_OUTCOMES for r in rows) else 0


def _persist_manifest(repo_root, slug, studio_branch, wt_path, base_branch, rows, meta, auto_commit) -> Path:
    """Write + commit the close-out manifest (the proof artifact)."""
    d = repo_root / ".court" / "studio-close" / slug
    d.mkdir(parents=True, exist_ok=True)
    path = d / "manifest.md"
    path.write_text(render_manifest(slug, studio_branch, wt_path or (repo_root / "(worktree gone)"), base_branch, rows, meta), encoding="utf-8")
    if auto_commit:
        try:
            git_ops.git_commit_paths([path], f"court: studio close manifest for {slug} ({meta.get('verdict')})")
        except Exception as e:
            print(f"⚠️  manifest commit failed ({e}); the file is on disk at {path}", file=sys.stderr)
    return path


def _teardown(repo_root, slug, wt_path, studio_branch, quests, as_json) -> dict:
    """Runserver kill -> worktree to Ashes (session alive) -> session stop.
    The studio branch ref is always kept."""
    info: dict = {"status": "done", "server": "", "cli_kills": [], "agent_manager": [], "branch_kept": studio_branch}
    if wt_path and wt_path.is_dir():
        info["server"] = stop_studio_server(repo_root, wt_path)
        info["cli_kills"] = stop_cli_artist_session(wt_path)
        session_ids = {getattr(q, "artist_session_id", "") or "" for q in quests}
        session_ids.discard("")
        info["agent_manager"] = teardown_agent_manager_commands(
            repo_root, wt_path, studio_branch, next(iter(session_ids), "")
        )
    else:
        info["status"] = "worktree already gone — nothing to stop or move"
    return info


def _print_close(slug, studio_branch, rows, meta, manifest, teardown_info, artist_spawn=None, as_json=False) -> None:
    if as_json:
        print(json.dumps({
            "studio": slug,
            "branch": studio_branch,
            "verdict": meta.get("verdict"),
            "rows": rows,
            "manifest": str(manifest),
            "teardown": teardown_info,
            "artist": artist_spawn or {},
        }, indent=2))
        return
    print("=" * 76)
    print(f"🏁 STUDIO CLOSE-OUT — {slug}  [{meta.get('verdict')}]")
    print("=" * 76)
    print(f"Studio branch: {studio_branch}  (ref kept)")
    print(f"Manifest:      {manifest}")
    for r in rows:
        icon = {"synced": "✅", "already-present": "✅", "union-pending": "🎨", "race-blocked": "🛡️ ",
                "refused": "🚫", "blocked": "⏸️ "}.get(r["outcome"], "•")
        print(f"  {icon} {r['id']}: {r['outcome']} — {r['provenance'] or '-'} | approved UI on branch: {r.get('ui_present') or '-'}")
    for n in meta.get("notes", []):
        print(f"  note: {n}")
    if teardown_info:
        print("-" * 76)
        status = teardown_info.get("status", "")
        if status:
            print(f"Teardown: {status}")
        if teardown_info.get("server"):
            print(f"  server: {teardown_info['server']}")
        for pid in teardown_info.get("cli_kills") or []:
            print(f"  stopped headless artist pid {pid}")
        for cmd in teardown_info.get("agent_manager") or []:
            print(f"  agent_manager: {cmd['op']} sessionID={cmd.get('sessionID', '-')} "
                  f"sectionID={cmd.get('sectionID', '-')} — {cmd.get('why', '')}")
    if artist_spawn:
        for k, v in artist_spawn.items():
            print(f"  artist {k}: {v}")
    print("=" * 76)
