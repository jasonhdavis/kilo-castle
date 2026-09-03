---
description: Move a completed/merged Quest worktree to Agent Manager Ashes section after merge and diff alignment
agent: steward
---
Quest: $ARGUMENTS

Follow the Raze Protocol:
1. **Target Identification & Merge Verification**:
   - For a single Quest:
     `python3 .court/engine/cli.py raze <id>`
   - For batch / all ready quests:
     `python3 .court/engine/cli.py raze all`
   - This deterministic CLI command:
     a) Verifies the Quest branch is merged into `castle` (or completed Scout report is stored).
     b) Fast-forwards / syncs the worktree branch with `castle` (`git merge castle --ff-only`) and confirms `git status --porcelain` is clean so that `ahead: 0, behind: 0` (zero drift, no warning triangles in UI).
     c) Advances Quest status to `READY_FOR_TEARDOWN`.
     d) Auto-archives any Quests whose worktrees were already deleted/pruned from disk and Agent Manager (`court archive <id>`).

2. **Move to Ashes Section in Agent Manager**:
   - Inspect the `raze` command output for the worktree's session ID and the Ashes section ID (`sec-...`).
   - Move the worktree session to the Ashes section using `agent_manager` `move`.
   - Never move broken, ghost, or unrelated worktrees to Ashes.
   - Never delete or stop the worktree directory directly — worktree removal is always M'Lord's manual action in the Agent Manager UI.

3. **Report to M'Lord**:
   - Run `python3 .court/engine/cli.py teardown-list` to display all active worktrees resting cleanly in Ashes awaiting M'Lord's final manual deletion.
