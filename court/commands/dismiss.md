---
description: Dismiss a stalled or confused Serf and dispatch a fresh Serf into the SAME worktree
agent: steward
---
Target: $ARGUMENTS

Follow the Dismiss Protocol:
1. Stop the stalled session in Agent Manager (`agent_manager` stop).
2. Retain the existing worktree and branch intact.
3. Dispatch a fresh Serf into the SAME worktree with corrected context.
4. Log the replacement in `.court/quests/<id>.md`.
