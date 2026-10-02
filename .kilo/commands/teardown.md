---
description: List worktrees ready for M'lord to manually prune in Agent Manager with deterministic merge verification
agent: steward
---
```bash
python3 -m court.cli teardown-list
```
Present the list plainly, highlighting each `READY_TO_RAZE` Quest's actual bucket (Ashes move
already reported/executed in Agent Manager vs. move still pending vs. already pruned
from disk/Agent Manager) and warning of any dirty worktrees. Never act on it beyond what's already been done
(reporting the `agent_manager` move/stop sequences from `/raze` for M'Lord to execute in the
Agent Manager UI — the tool itself is permission-denied for every agent in this project) — actual
worktree deletion is always M'lord's manual action in the Agent Manager UI.

