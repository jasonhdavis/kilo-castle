---
description: List worktrees ready for M'lord to manually prune in Agent Manager with deterministic merge verification
agent: steward
---
```bash
python3 -m court.cli teardown-list
```
Present the list plainly, highlighting each `READY_TO_RAZE` Quest's actual bucket (already
moved to Agent Manager's Ashes section vs. still sitting in another lane vs. already pruned
from disk/Agent Manager) and warning of any dirty worktrees. Never act on it beyond what's already been done
(moving the worktree to Agent Manager's **Ashes** section via `/raze`) — actual
worktree deletion is always M'lord's manual action in the Agent Manager UI.

