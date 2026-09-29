---
description: Move a completed/merged Quest worktree to the Ashes holding area after merge and diff alignment
agent: steward
---
Quest: $ARGUMENTS

Follow the Raze Protocol:
1. **Target Identification & Merge Verification**:
   - For a single Quest:
     `python3 -m court.cli raze <id>`
   - For batch / all ready quests (the `raze all` magic string is retired — omit
     `quest_ids` and rely on the default status filter, or narrow it explicitly):
     `python3 -m court.cli raze` (defaults to `GATE,READY_TO_RAZE,TRIBUTE_READY`)
     or `python3 -m court.cli raze --status READY_TO_RAZE`
   - This deterministic CLI command:
     a) Verifies the Quest branch is merged into `castle` (or completed Scout report is stored).
     b) Fast-forwards / syncs the worktree branch with `castle` (`git merge castle --ff-only`) and confirms `git status --porcelain` is clean so that `ahead: 0, behind: 0` (zero drift, no warning triangles in UI).
     c) Advances Quest status to `READY_TO_RAZE`. (A `PUNISHED` Quest is never razed alone — its joint teardown with its successor happens automatically once the successor reaches `READY_TO_RAZE`/`DONE`; see `/pillory`.)
     d) Auto-archives any Quests whose worktrees were already deleted/pruned from disk (`court archive <id>`).

2. **Park the Worktree (Ashes)**:
   - Inspect the `raze` command output for the worktree's session ID and cleanup hints.
   - Stop the idle session via Kilo CLI if needed (`kilo session delete <sessionID>`).
   - Never park broken, ghost, or unrelated worktrees.
   - Never delete the worktree directory directly — worktree removal is always M'Lord's manual action.

3. **Report to M'Lord**:
   - Run `python3 -m court.cli teardown-list` to display all active worktrees resting cleanly in Ashes awaiting M'Lord's final manual deletion.

4. **Fork Teardown Sweep — Master of Coin & Gatekeeper scaffolding (always run this too)**:
   - Run `python3 -m court.cli fork-teardown-list`. This is a *different* object than the
     Quest worktrees above: MoC audit sessions and Gatekeeper convoys run in disposable forked
     worktrees created for the run and torn down after (see `AGENTS.md` "Master of Coin /
     Gatekeeper Worktree Forking..."). They are never Quests themselves, so `teardown-list`
     above cannot see them.
   - **ELIGIBLE entries** (verdict/promotion already confirmed synced onto the real branch): for each
     one with a live session, stop it via Kilo CLI: `kilo session delete <sessionID>`.
   - **Never run `git worktree remove` / delete the directory yourself** — that desyncs session
     bookkeeping into a stale entry. Physical directory deletion is always M'Lord's manual
     action, exactly like the Quest-worktree flow above — this command only ever tells you
     what to clean up, never deletes anything itself.
   - **HOLD entries** (verdict not yet synced back): do not touch the worktree. If a live session
     is listed, deliver the printed re-prompt verbatim into that worktree via Kilo CLI
     (`kilo run --agent master_of_coin --model "$(python3 -m court.cli model master_of_coin)" --dir <wt>`)
     so the stalled MoC/Gatekeeper session pushes its sync-back itself. If no session remains, the audit stalled
     without ever finishing — leave that fork alone and re-dispatch a fresh session per
     `master_of_coin_review_prompt.md` / `gatekeeper_review_prompt.md` instead of resurrecting it.
