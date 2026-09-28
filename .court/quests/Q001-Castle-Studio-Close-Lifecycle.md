---
id: Q001-Castle-Studio-Close-Lifecycle
title: Studio close-out lifecycle: gated close command with sign-off proof, drift/race guards, cherry-pick extraction, union briefs, close-out manifest, teardown
kind: quest
app: castle
concern: studio-close-lifecycle
parent_epic: 
section: 
tags: 
status: OPEN
cogship_id: 
cogship_station: 
cogship_promoted_commit: 
task_file: 
pillory_of: 
pilloried_by: 
scout_of: 
branch: quest/q001-castle-studio-close-lifecycle
worktree: 
serf_session_id: 
serf_model: 
master_of_coin_session_id: 
master_of_coin_model: 
gatekeeper_session_id: 
gatekeeper_model: 
artist_session_id: 
artist_model: 
vassal_session_id: 
created_at: 2026-09-28T03:00:12Z
updated_at: 2026-09-28T03:00:12Z
---

# Q001-Castle-Studio-Close-Lifecycle — Studio close-out lifecycle: gated close command with sign-off proof, drift/race guards, cherry-pick extraction, union briefs, close-out manifest, teardown

## Castle Ledger

- **2026-09-28T03:00:12Z** — - → OPEN: Quest created

## The Kingdom Requires

`court studio <ids> --close` — a deterministic close-out command that owns the entire back half of the combined-studio lifecycle, leaving `court studio <ids>` (standup) as the front half. All engine code lives in the `court/` package (source of truth `.court/engine/`), riding the normal pipeline. A new thin slash command `.kilo/commands/studio-close.md` wraps it.

Tonight's studio close-out had no gated lifecycle: sign-off state lived only in commit messages and `.kilo/TASK_ARTIST.md` prose; sync-back merged a studio branch cut from a ~2000-commit-drifted base (replaying ~19k stale lines into 5 quest branches); a gatekeeper convoy merged two tainted branches before being killed mid-merge (no race referee); whole-branch merge hid that one quest's entire chain existed only on the studio branch; and conflicting badge-overlap quests would have needed blind union resolutions that regress shipped features. This command closes all five gaps.

## Command surface
```
python3 -m court.cli studio <ids> --close [--signoff "<note>"] [--force-union]
    [--drift-threshold N] [--override-manifest] [--skip-teardown] [--artist-session] [--json] [--no-commit]
```
Phase order: (1) guards → (2) extraction + close-out manifest → (3) teardown. Any guard refusal exits non-zero with a per-quest reason table; `--json` emits machine-readable per-quest outcomes. The cohort's studio branch/worktree/session are derived the same way standup derives them (`artist/<slug>-ui-studio`, worktree `artist-studio-<slug>`, `artist_session_id` frontmatter).

## Guard 1 — sign-off proof
Close refuses unless every cohort quest carries a dated royal sign-off. Two accepted proofs:
- (a) the quest's Castle Ledger trail contains a dated line with the canonical marker `studio sign-off` (case-insensitive) — the stamp a prior royal review session writes; or
- (b) `--signoff "<note>"` is passed at invocation, which appends a dated `studio sign-off: <note>` ledger entry to every cohort quest.
Missing → refuse with a per-quest listing; nothing is extracted, no teardown.

## Guard 2 — base-drift
Measure the studio branch's base drift: `git rev-list --count $(git merge-base <studio_branch> castle)..castle`. Default threshold 100 commits, overridable per-invocation via `--drift-threshold` and durable via `studio.close_max_base_drift` in `.court/config.json`. Beyond threshold: refuse and recommend a re-cut instead, unless `--force-union`. This guard also applies to `--sync-back` (the low-level primitive keeps its manual role but loses its unguarded footgun).

## Guard 3 — convoy-race
For each cohort quest, refuse when the quest is already in an integration lane: frontmatter `cogship_id` stamped with a still-existing `the-gatehouse/<id>` branch, OR the quest branch tip is an ancestor of (contained in) any existing `the-gatehouse/*` branch. Applies to `--sync-back` too. Race-blocked quests get no extraction; their manifest row records the blocking convoy branch.

## Guard 4 — cherry-pick extraction, not branch-merge
On close, extract the artist's labeled commits per quest — commits on the studio branch whose subject matches the established polish convention (`style(...): <QID> ... royal review ...`, case-insensitive) and/or carry an `Addendum-Quests:` trailer naming the quest — and cherry-pick them onto the quest branch in topological order (clean tree required, `git cherry-pick -x` to record provenance, hashes captured for the manifest). Any conflict aborts that quest's cherry-pick cleanly and routes it to Guard 5 — never auto-union, never force. Quests whose labeled commits all already exist on the branch (patch-id equivalence, e.g. `git cherry`) are verified no-ops ("already-present"), never re-merged. This catches the tonight-failure class where a quest's approved chain existed only on the studio branch. `--sync-back` (whole-branch merge) remains available as the guarded low-level primitive.

## Guard 5 — conflicts route to the artist with a union brief
For a quest whose extraction conflicts, generate a machine-written brief at `.court/studio-close/<studio-slug>/<qid>-union-brief.md`: (a) each conflicted file with its conflict regions and both sides' content; (b) the functional features the approved design predates — computed by diffing branch tip vs studio tip (files changed on the quest branch since merge-base that the studio tip lacks, and the studio-side changes the branch lacks). Mark the quest with a dated `Studio Close: UNION-PENDING` ledger entry. With `--artist-session`, stand up a dedicated artist session in the (still-live) studio worktree with the brief; otherwise print the ready-to-run spawn command. Conflicting quests are never union-resolved by the close path.

## The close-out manifest (proof artifact)
Rendered like the ship manifest and committed with the paperwork at `.court/studio-close/<studio-slug>/manifest.md`: per cohort quest → `synced` (with cherry-pick commit hashes), `union-pending` (brief path), `already-present` (verified no-op), or `race-blocked` (blocking convoy branch) — plus a per-quest "approved UI present on branch: YES/NO" check (file-level: every labeled artist commit's patch-id applied on the branch tip, or the touched files identical between branch tip and studio tip). Teardown is unlocked only when the manifest is all-green or `--override-manifest` is passed.

## Teardown (phase 3, only post-manifest)
Kill the studio runserver(s) (`.kilo/manage_servers.sh` stop semantics / `.worktree-port`), move the studio worktree to the Ashes section in `agent-manager.json` while its session still lives, then stop the artist session — respecting the move-before-stop ordering. Keep the studio branch ref: history is cheap, the worktree is the clutter. Physical deletion stays a manual action. `--skip-teardown` stops after the manifest.

## Expected Tribute

- [ ] `court studio <ids> --close` implemented in the engine (deterministic Python, stdlib-only, matching existing `cmd_studio` conventions) with phase order guards → extraction+manifest → teardown, per-quest refusal reasons, and non-zero exit on any refusal
- [ ] Guard 1 — sign-off proof: dated ledger-marker detection plus `--signoff` writer; refusal path unit-verified against real quest-file fixtures
- [ ] Guard 2 — base-drift via merge-base distance; threshold from `--drift-threshold` / `studio.close_max_base_drift` config (default 100); also enforced on `--sync-back`; `--force-union` override
- [ ] Guard 3 — convoy-race detection (stamped cogship with live `the-gatehouse/<id>` branch, or quest tip contained in any `the-gatehouse/*` branch); also enforced on `--sync-back`
- [ ] Guard 4 — cherry-pick extraction of labeled artist commits (`style(...): <QID> ... royal review ...` subject + `Addendum-Quests:` trailer) with `-x` provenance, clean-tree requirement, conflict-abort (never auto-union), and patch-id "already-present" verification; `--sync-back` retained as guarded low-level primitive
- [ ] Guard 5 — union-brief generator writing `.court/studio-close/<slug>/<qid>-union-brief.md` (conflict regions + predates-features diff both directions) + dated `Studio Close: UNION-PENDING` ledger marking + `--artist-session` standup option
- [ ] Close-out manifest rendered and committed at `.court/studio-close/<slug>/manifest.md` with per-quest outcome (synced + hashes / union-pending / already-present / race-blocked) and per-quest approved-UI-present YES/NO check; teardown gated on all-green or `--override-manifest`
- [ ] Teardown sequence: runserver kill → studio worktree moved to Ashes (session still alive) → session stop; studio branch ref kept; physical deletion left manual
- [ ] `.kilo/commands/studio-close.md` thin wrapper: run the close, read the manifest, prompt only for union-pending rulings
- [ ] Deterministic tests (extend `tests/test_studio.py` or add `tests/test_studio_close.py`) covering each guard's accept + refuse paths against fixture git repos; full suite `python3 -m pytest tests -q` green
- [ ] Non-goals respected: no atelier close support (follow-up), no standup behavior change, no duplication of Q538/Q696 paperwork work

- [ ] **Self-advance (do this LAST)**: once Tribute is rendered and `git rev-list --count HEAD..castle` is 0, run `python3 -m court.cli advance Q001-Castle-Studio-Close-Lifecycle TRIBUTE_READY` yourself. Nothing else flips the status out of WORKING.

## Tribute Rendered

## Master of Coin's Audit

## Judgement of the Condemned

## Cogship Log

## Audience Log
