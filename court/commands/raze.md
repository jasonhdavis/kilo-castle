---
description: Move completed or obsolete worktrees to Ashes section and queue for manual teardown
agent: steward
---
Quest: $ARGUMENTS

Follow the Raze Protocol:
1. Identify the target Quest (`court show <id>`).
2. Verify that the Quest is in `READY_FOR_TEARDOWN` or has been approved for deprecation.
3. Move the worktree in Agent Manager to the **Ashes** section (`agent_manager` `move`) as a visual cue.
4. If M'Lord confirms deletion, surface the worktree path for manual teardown in the Agent Manager UI.
5. Note: The Steward NEVER forcibly deletes worktrees out from under Agent Manager; pruning is confirmed in the UI.
