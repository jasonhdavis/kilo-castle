---
description: List worktrees ready for M'lord to manually prune with deterministic merge verification
agent: steward
---
```bash
python3 -m court.cli teardown-list
```
Present the list plainly, highlighting each `READY_TO_RAZE` Quest's actual bucket (already
parked after raze vs. still sitting in another lane vs. already pruned
from disk) and warning of any dirty worktrees. Never act on it beyond what's already been done
(razed via `/raze`) — actual
worktree deletion is always M'lord's manual action.

