---
description: Move a completed/merged Quest worktree to Agent Manager Ashes section after merge and diff alignment
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
     d) Auto-archives any Quests whose worktrees were already deleted/pruned from disk and Agent Manager (`court archive <id>`).

2. **Move to Ashes Section in Agent Manager**:
   - Inspect the `raze` command output for the worktree's session ID and the Ashes section ID (`sec-...`).
   - Move the worktree session to the Ashes section using `agent_manager` `move`.
   - Never move broken, ghost, or unrelated worktrees to Ashes.
   - Never delete or stop the worktree directory directly — worktree removal is always M'Lord's manual action in the Agent Manager UI.

3. **Report to M'Lord**:
   - Run `python3 -m court.cli teardown-list` to display all active worktrees resting cleanly in Ashes awaiting M'Lord's final manual deletion.

4. **Fork Teardown Sweep — Master of Coin & Gatekeeper scaffolding (always run this too)**:
   - Run `python3 -m court.cli fork-teardown-list`. This is a *different* object than the
     Quest worktrees above: MoC audit sessions and Gatekeeper convoys run in disposable forked
     worktrees Agent Manager created because it can only ever create, never attach to an existing
     one (see `AGENTS.md` "Master of Coin / Gatekeeper Worktree Forking..."). They are never Quests
     themselves, so `teardown-list` above cannot see them.
   - **ELIGIBLE entries** (verdict/promotion already confirmed synced onto the real branch): for each
     one with a live session, run, **in this exact order**:
     1. `agent_manager` `move` → the printed target section (**The Treasury** for Master of Coin
        forks, **Ashes** for Gatekeeper convoys). This step needs the live `ses_...` session ID —
        it fails once the session is gone.
     2. `agent_manager` `stop` on that same session, **only after** the move above has landed.
   - **Never call `agent_manager stop` before the move, and never run `git worktree remove` /
     delete the directory yourself** — the former makes the worktree permanently un-movable by this
     tool (stranded in whatever section it was already in), and the latter desyncs Agent Manager's
     own bookkeeping into a stale, unkillable session entry. Physical directory deletion is always
     M'Lord's manual action in the Agent Manager UI, exactly like the Quest-worktree Ashes flow
     above — this command only ever tells you what to move/stop, never deletes anything itself.
   - **HOLD entries** (verdict not yet synced back): do not touch the worktree. If a live session
     is listed, send the printed `agent_manager prompt` re-prompt verbatim so the stalled MoC/
     Gatekeeper session pushes its sync-back itself. If no session remains, the audit stalled
     without ever finishing — leave that fork alone and re-dispatch a fresh session per
     `master_of_coin_review_prompt.md` / `gatekeeper_review_prompt.md` instead of resurrecting it.
