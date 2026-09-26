"""
Castle Branch-Protection Guard (technical enforcement of the
"no direct-to-castle code commits" rule).

POLICY
======
On a direct (non-gatehouse) commit to the `castle` branch:

* WHITELIST  — Steward bookkeeping/process text only, no code or
  executable-config semantics. Allowed on a direct `castle` commit.
* BLACKLIST  — Serf-worktree-only paths (anything with code or
  executable-config semantics). Always rejected on a direct `castle` commit.
* UNCLASSIFIED — every path in neither list (e.g. `templates/**`, `core/**`,
  `config/**`, `docs/**`, `manage.py`, root `*.py`/`*.sh`/`Dockerfile*`/
  `*.toml`/`*.yml`). Default-deny: rejected on a direct `castle` commit.
  Dispatch a Serf or charter the change instead.

EXEMPTIONS
==========
 * Fast-forward promotions (`git merge the-gatehouse/<cogship> --ff-only` on
   `castle`) create no commit, so no hook fires and nothing is blocked.
 * A merge commit on `castle` whose merged-in head is already contained in a
   live `the-gatehouse/*` branch history (a Cog Ship promotion) is allowed.
 * Commits on any non-`castle` branch (Serf quest worktrees, gatehouse
   convoy worktrees, ward/epic branches) are never subject to this guard.

PUSH POLICY (pre-push)
======================
Pipeline-internal refs never leave the local machine, and the trunk mirror
on a hosted remote can only ever track the real local `castle`:

 * Local-path remotes (`git push . HEAD:<branch>`, worktree-to-worktree
   sync) are always allowed — the Master of Coin's local sync-back depends
   on this.
 * On hosted (URL) remotes:
   * Pushing any `quest/*`, `scout/*`, or `the-gatehouse/*` branch to a
     hosted remote is rejected — pipeline internals are local-only
     (cogship incident: a quest branch pushed straight onto origin/castle).
   * Updating a hosted `castle` ref from anything other than the local
     `castle` branch is rejected (no branch laundering onto the trunk ref).
   * Non-fast-forward updates of a hosted `castle` ref (history rewrites)
     are rejected; fast-forward realignment and first creation are allowed.

ENFORCEMENT POINTS
==================
* `.githooks/pre-commit`         — plain `git commit` on `castle`.
* `.githooks/pre-merge-commit`   — non-fast-forward `git merge` into `castle`.
* `.githooks/pre-push`           — pipeline-ref / trunk-mirror push policy.
* `castle_guard.py audit-castle` — post-hoc history scan that also catches
  `git commit --no-verify` bypasses.

ACTIVATION
==========
    python3 -m court.castle_guard install     # sets core.hooksPath=.githooks
    python3 -m court.castle_guard status      # verify
"""
from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Policy constants
# ---------------------------------------------------------------------------

STEWARDSHIP_WHITELIST: tuple[str, ...] = (
    ".court/quests/**",
    ".court/epics/**",
    ".court/scouts/**",
    ".court/archive/**",
    ".court/ward/**",
    ".court/LEDGER.md",
    ".court/GLOSSARY.md",
    ".court/EDICTS.md",
    "tasks/**",
    "AGENTS.md",
)

SERF_ONLY_BLACKLIST: tuple[str, ...] = (
    ".court/engine/**",
    "court/**",
    "apps/**",
    "tests/**",
    "scripts/**",
    "src/**",
    "kilo.json",
    ".kilo/command*/**",
    ".kilo/prompt*/**",
    ".kilo/agent*/**",
)

GATEHOUSE_PREFIX: str = "the-gatehouse/"
GUARDED_BRANCHES: tuple[str, ...] = ("castle",)

# Branch namespaces that exist only inside the local machine (Serf quest
# worktrees, scout spikes, ephemeral gatehouse convoys). They must never be
# pushed to a hosted remote.
PIPELINE_REF_PREFIXES: tuple[str, ...] = ("quest/", "scout/", GATEHOUSE_PREFIX)

TRUNK_BRANCH: str = "castle"
ZERO_SHA: str = "0" * 40

WHITELIST = "whitelist"
BLACKLIST = "blacklist"
UNCLASSIFIED = "unclassified"

EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _git(args: list[str], cwd: Optional[Path] = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git"] + args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout.rstrip("\n"), proc.stderr.rstrip("\n")


def current_branch(cwd: Optional[Path] = None) -> str:
    rc, out, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if rc != 0:
        return ""
    branch = out.strip()
    return "" if branch == "HEAD" else branch


def is_guarded_branch(branch: str) -> bool:
    return branch in GUARDED_BRANCHES


def merge_head_sha(cwd: Optional[Path] = None) -> Optional[str]:
    rc, out, _ = _git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd)
    if rc == 0 and out.strip():
        return out.strip()

    rc, git_dir, _ = _git(["rev-parse", "--git-dir"], cwd)
    base = Path(cwd) if cwd else Path.cwd()
    gdir = Path(git_dir) if rc == 0 and git_dir.strip() else base / ".git"
    if not gdir.is_absolute():
        gdir = base / gdir

    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REBASE_HEAD"):
        p = gdir / name
        if p.is_file():
            text = p.read_text(encoding="utf-8").strip()
            if text:
                return text.splitlines()[0].strip()
    return None


def gatehouse_branches(cwd: Optional[Path] = None) -> list[str]:
    rc, out, _ = _git(
        ["for-each-ref", "--format=%(refname:short)", f"refs/heads/{GATEHOUSE_PREFIX}*"],
        cwd,
    )
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def station_containing(sha: str, cwd: Optional[Path] = None) -> Optional[str]:
    for station in gatehouse_branches(cwd):
        rc, _, _ = _git(["merge-base", "--is-ancestor", sha, station], cwd)
        if rc == 0:
            return station
    return None


def _match_pattern(pattern: str, path: str) -> bool:
    if pattern.endswith("/**"):
        base = pattern[: -len("/**")]
        if not fnmatch.fnmatch(path, base):
            return path.startswith(_strip_wildcard_base(base) + "/") or fnmatch.fnmatch(
                path, base + "/*"
            )
        return True
    return fnmatch.fnmatch(path, pattern)


def _strip_wildcard_base(base: str) -> str:
    if "*" in base:
        return base[: base.index("*")]
    return base


def classify_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    for pattern in SERF_ONLY_BLACKLIST:
        if _match_pattern(pattern, normalized):
            return BLACKLIST
    for pattern in STEWARDSHIP_WHITELIST:
        if _match_pattern(pattern, normalized):
            return WHITELIST
    return UNCLASSIFIED


def staged_paths(cwd: Optional[Path] = None) -> list[str]:
    rc, out, _ = _git(["diff", "--cached", "--name-only", "--no-renames"], cwd)
    if rc != 0:
        rc, out, _ = _git(
            ["diff", "--cached", "--name-only", "--no-renames", EMPTY_TREE_SHA], cwd
        )
        if rc != 0:
            return []
    return [line for line in out.splitlines() if line.strip()]


def changed_paths_of_commit(sha: str, cwd: Optional[Path] = None) -> list[str]:
    rc, _, _ = _git(["rev-parse", "--verify", f"{sha}^1"], cwd)
    base = f"{sha}^1" if rc == 0 else EMPTY_TREE_SHA
    rc, out, _ = _git(["diff", "--name-only", "--no-renames", base, sha], cwd)
    if rc != 0:
        return []
    return [line for line in out.splitlines() if line.strip()]


def commit_parents(sha: str, cwd: Optional[Path] = None) -> list[str]:
    rc, out, _ = _git(["rev-list", "--parents", "-n", "1", sha], cwd)
    if rc != 0 or not out.strip():
        return []
    return out.strip().split()[1:]


def _decision(
    allowed: bool,
    reason: str,
    offenders: Optional[list[dict]] = None,
) -> dict:
    return {
        "allowed": allowed,
        "reason": reason,
        "offenders": offenders or [],
    }


def _classify_staged(paths: Iterable[str]) -> dict:
    offenders: list[dict] = []
    for path in paths:
        verdict = classify_path(path)
        if verdict == WHITELIST:
            continue
        if verdict == BLACKLIST:
            offenders.append(
                {
                    "path": path,
                    "verdict": BLACKLIST,
                    "why": "Serf-worktree-only path (code or executable-config semantics)",
                }
            )
        else:
            offenders.append(
                {
                    "path": path,
                    "verdict": UNCLASSIFIED,
                    "why": "not on the Steward-direct whitelist; default-deny on castle",
                }
            )
    if offenders:
        blacklist_hits = [o["path"] for o in offenders if o["verdict"] == BLACKLIST]
        unclassified_hits = [o["path"] for o in offenders if o["verdict"] == UNCLASSIFIED]
        parts = []
        if blacklist_hits:
            parts.append("Serf-worktree-only paths: " + ", ".join(blacklist_hits))
        if unclassified_hits:
            parts.append("non-whitelisted paths: " + ", ".join(unclassified_hits))
        return _decision(
            False,
            "Direct-to-castle commit rejected by castle branch guard ("
            + "; ".join(parts)
            + "). Dispatch a Serf worktree (quest/* -> the-gatehouse/<cogship> -> castle) "
            "for anything with code or executable-config semantics.",
            offenders,
        )
    return _decision(True, "all staged paths are Steward-direct-safe bookkeeping")


def evaluate_pending_commit(hook_name: str = "pre-commit", cwd: Optional[Path] = None) -> dict:
    branch = current_branch(cwd)
    if not is_guarded_branch(branch):
        return _decision(True, f"branch {branch or '(detached HEAD)'} is not guarded")

    if hook_name == "pre-merge-commit":
        rc, out, _ = _git(["reflog", "-n", "5"], cwd)
        if rc == 0 and GATEHOUSE_PREFIX in out:
            return _decision(
                True,
                "gatehouse promotion merge: reflog indicates a the-gatehouse/* source",
            )
        
        rc_git, git_dir, _ = _git(["rev-parse", "--git-dir"], cwd)
        if rc_git == 0 and git_dir.strip():
            base = Path(cwd) if cwd else Path.cwd()
            gdir = Path(git_dir)
            if not gdir.is_absolute():
                gdir = base / gdir
            for filename in ("COMMIT_EDITMSG", "MERGE_MSG", "AUTO_MERGE"):
                p = gdir / filename
                if p.is_file():
                    content = p.read_text(encoding="utf-8", errors="ignore")
                    if GATEHOUSE_PREFIX in content:
                        return _decision(
                            True,
                            f"gatehouse promotion merge: {filename} references a the-gatehouse/* source",
                        )
        
        return _decision(
            False,
            "Direct-to-castle merge rejected by castle guard: merge source is "
            "not a the-gatehouse/* branch. Route through a gatehouse branch "
            "(quest/* -> the-gatehouse/<cogship> -> castle).",
            [{"path": "merge-source", "verdict": BLACKLIST, "why": "ungated merge source"}],
        )

    return _classify_staged(staged_paths(cwd))


def evaluate_existing_commit(sha: str, cwd: Optional[Path] = None) -> dict:
    branch_ref = f"refs/heads/{GUARDED_BRANCHES[0]}"
    rc, _, _ = _git(["merge-base", "--is-ancestor", sha, branch_ref], cwd)
    if rc != 0:
        return _decision(True, f"commit {sha[:12]} is not on {GUARDED_BRANCHES[0]}")

    parents = commit_parents(sha, cwd)
    if len(parents) > 1:
        ungated = [p for p in parents[1:] if not station_containing(p, cwd)]
        if ungated:
            return _decision(
                False,
                f"merge commit {sha[:12]} on castle has non-gatehouse source(s): "
                + ", ".join(p[:12] for p in ungated),
                [{"path": f"parent={p[:12]}", "verdict": BLACKLIST, "why": "ungated merge source"} for p in ungated],
            )
        return _decision(
            True,
            "gatehouse promotion merge: all non-first parents are contained in "
            "live the-gatehouse/* histories",
        )

    return _classify_staged(changed_paths_of_commit(sha, cwd))


def audit_castle(cwd: Optional[Path] = None, max_count: int = 50) -> list[dict]:
    violations: list[dict] = []
    for branch in GUARDED_BRANCHES:
        rc, out, _ = _git(["rev-list", "--first-parent", "-n", str(max_count), branch], cwd)
        if rc != 0:
            continue
        for sha in out.split():
            res = evaluate_existing_commit(sha, cwd)
            if not res["allowed"]:
                res["sha"] = sha
                res["branch"] = branch
                res["subject"] = _git(
                    ["log", "-n", "1", "--format=%s", sha], cwd
                )[1]
                violations.append(res)
    return violations


def is_local_remote_url(url: str) -> bool:
    """True when a push destination is a local path (same repo, another
    worktree, or a file:// URL). Local sync between local refs is always
    permitted — only hosted (URL) remotes are subject to the push policy."""
    u = (url or "").strip()
    if not u:
        return True
    if u.startswith("file://"):
        return True
    if u in (".", ".."):
        return True
    if u.startswith(("/", "./", "../")):
        return True
    return False


def _short_ref(ref: str) -> str:
    for prefix in ("refs/heads/", "refs/tags/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def evaluate_pre_push(
    lines: Iterable[str],
    remote_url: str,
    cwd: Optional[Path] = None,
) -> dict:
    """Evaluate a `pre-push` stdin payload (one `<local-ref> <local-sha>
    <remote-ref> <remote-sha>` line per pushed ref) against the push policy.

    Local-path remotes are always allowed. Hosted remotes reject pushes of
    pipeline-internal branches (quest/*, scout/*, the-gatehouse/*), any
    update of the hosted `castle` ref from a non-castle local branch, and
    non-fast-forward rewrites of the hosted `castle` ref.
    """
    if is_local_remote_url(remote_url):
        return _decision(True, f"local remote ({remote_url or 'local path'}) — push policy not applied")

    offenders: list[dict] = []
    for line in lines:
        parts = str(line).split()
        if len(parts) == 4:
            src_ref, src_sha, dst_ref, dst_sha = parts
        elif len(parts) == 2:
            src_ref, src_sha, dst_ref, dst_sha = "", "", parts[0], parts[1]
        else:
            continue

        src_name = _short_ref(src_ref) if src_ref else ""
        dst_name = _short_ref(dst_ref)

        if not src_ref:  # remote ref deletion
            if dst_name == TRUNK_BRANCH or dst_name.startswith(PIPELINE_REF_PREFIXES):
                offenders.append(
                    {
                        "path": f"delete {dst_name}",
                        "verdict": BLACKLIST,
                        "why": "deleting a pipeline-internal or trunk ref on a hosted remote is forbidden",
                    }
                )
            continue

        if src_ref.startswith("refs/heads/") and src_name.startswith(PIPELINE_REF_PREFIXES):
            offenders.append(
                {
                    "path": f"{src_name} -> {dst_name}",
                    "verdict": BLACKLIST,
                    "why": "pipeline-internal branches (quest/*, scout/*, the-gatehouse/*) never leave the local machine",
                }
            )
            continue

        if dst_name == TRUNK_BRANCH:
            if src_name != TRUNK_BRANCH:
                offenders.append(
                    {
                        "path": f"{src_name or src_sha[:12]} -> {dst_name}",
                        "verdict": BLACKLIST,
                        "why": "only the local castle branch may update a hosted castle ref (no branch laundering onto the trunk)",
                    }
                )
                continue
            if dst_sha != ZERO_SHA:
                rc, _, _ = _git(
                    ["merge-base", "--is-ancestor", dst_sha, src_sha], cwd
                )
                if rc != 0:
                    offenders.append(
                        {
                            "path": f"{src_name} -> {dst_name}",
                            "verdict": BLACKLIST,
                            "why": "non-fast-forward rewrite of a hosted castle ref is forbidden (fast-forward realignment only)",
                        }
                    )

    if offenders:
        return _decision(
            False,
            "push rejected by castle push guard: pipeline refs are local-only "
            "and hosted castle may only fast-forward from local castle. "
            "Promotion is local (git merge the-gatehouse/<cogship> --ff-only); "
            "remote synchronization and deployment belong to the Steward/human.",
            offenders,
        )
    return _decision(True, "all pushed refs satisfy the castle push policy")


def run_pre_push_hook(
    remote_name: Optional[str],
    stdin_lines: Optional[list[str]] = None,
    cwd: Optional[Path] = None,
    url_fallback: Optional[str] = None,
) -> dict:
    """Resolve the push destination URL and evaluate the pre-push payload."""
    if stdin_lines is None:
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
        stdin_lines = [ln for ln in raw.splitlines() if ln.strip()]
    url = remote_name or url_fallback or ""
    if remote_name:
        rc, out, _ = _git(["remote", "get-url", remote_name], cwd)
        url = out.strip() if rc == 0 and out.strip() else (url_fallback or remote_name)
    return evaluate_pre_push(stdin_lines or [], url, cwd=cwd)


def _repo_config_get(key: str, cwd: Optional[Path] = None) -> str:
    rc, out, _ = _git(["config", "--get", key], cwd)
    return out.strip() if rc == 0 else ""


def install(cwd: Optional[Path] = None) -> int:
    rc, _, err = _git(["config", "core.hooksPath", ".githooks"], cwd)
    if rc != 0:
        print(f"FAILED to set core.hooksPath: {err}", file=sys.stderr)
        return 1
    print("castle guard installed: core.hooksPath = .githooks (all worktrees).")
    print("Verify with: python3 -m court.castle_guard status")
    return 0


def status(cwd: Optional[Path] = None) -> int:
    hooks_path = _repo_config_get("core.hooksPath", cwd)
    engine = Path(__file__).resolve()
    print(f"core.hooksPath   : {hooks_path or '(unset — guard INERT)'}")
    print(f"guard module     : {engine}")
    print(f"guarded branches : {', '.join(GUARDED_BRANCHES)}")
    print(
        "push policy      : pipeline refs ("
        + ", ".join(PIPELINE_REF_PREFIXES)
        + ") local-only; hosted castle fast-forward-from-castle only"
    )
    live_gatehouses = gatehouse_branches(cwd)
    print(
        f"gatehouse prefix : {GATEHOUSE_PREFIX}* "
        f"(live branches: {', '.join(live_gatehouses) or 'none'})"
    )
    print(f"whitelist entries: {len(STEWARDSHIP_WHITELIST)}")
    print(f"blacklist entries: {len(SERF_ONLY_BLACKLIST)}")
    return 0


def _fmt_decision(res: dict) -> str:
    if res["allowed"]:
        return f"ALLOW: {res['reason']}"
    lines = [f"REJECT: {res['reason']}"]
    for o in res.get("offenders", []):
        lines.append(f"  - {o['path']}  [{o['verdict']}]  {o['why']}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="castle_guard",
        description="Castle branch-protection guard (deterministic, stdlib-only).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_hook = sub.add_parser("hook", help="hook entry point (used by .githooks/*)")
    p_hook.add_argument("hook_name", choices=["pre-commit", "pre-merge-commit", "pre-push"])
    p_hook.add_argument(
        "remote", nargs="?", default=None, help="remote name or URL (pre-push only)"
    )
    p_hook.add_argument(
        "remote_url", nargs="?", default=None, help="remote URL (pre-push $2, informational)"
    )

    p_check = sub.add_parser("check-commit", help="evaluate an existing commit against the castle policy")
    p_check.add_argument("sha")

    p_audit = sub.add_parser("audit-castle", help="scan castle history for policy violations")
    p_audit.add_argument("--max-count", type=int, default=50)

    p_explain = sub.add_parser("explain", help="classify paths (whitelist/blacklist/unclassified)")
    p_explain.add_argument("paths", nargs="+")

    sub.add_parser("install", help="activate the hooks (core.hooksPath=.githooks)")
    sub.add_parser("status", help="show guard activation and policy summary")

    args = parser.parse_args(argv)

    if args.cmd == "hook":
        if args.hook_name == "pre-push":
            res = run_pre_push_hook(
                args.remote, url_fallback=getattr(args, "remote_url", None)
            )
        else:
            res = evaluate_pending_commit(hook_name=args.hook_name)
        print(_fmt_decision(res))
        return 0 if res["allowed"] else 1
    if args.cmd == "check-commit":
        res = evaluate_existing_commit(args.sha)
        print(_fmt_decision(res))
        return 0 if res["allowed"] else 1
    if args.cmd == "audit-castle":
        violations = audit_castle(max_count=args.max_count)
        if not violations:
            print("audit-castle: no violations found in scanned history.")
            return 0
        print(f"audit-castle: {len(violations)} violating commit(s) on guarded branches:")
        for v in violations:
            print(f"  {v['sha'][:12]}  {v.get('subject', '')}")
            print(f"    {v['reason']}")
        return 1
    if args.cmd == "explain":
        for path in args.paths:
            print(f"{classify_path(path):<14} {path}")
        return 0
    if args.cmd == "install":
        return install()
    if args.cmd == "status":
        return status()
    return 2


if __name__ == "__main__":
    sys.exit(main())
