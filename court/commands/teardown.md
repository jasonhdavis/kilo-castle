---
description: List worktrees ready for M'lord to manually prune in Agent Manager (Ashes)
agent: steward
---
```
python3 .court/engine/cli.py teardown-list
```
Present the list plainly. Never act on it beyond what's already been done
(moving the worktree to Agent Manager's **Ashes** section via `/raze`) — actual
worktree deletion is always M'lord's manual action in the Agent Manager UI.
