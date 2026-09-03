---
description: Send a Quest back to WORKING with a specific reason
agent: steward
---
Arguments: $ARGUMENTS (expected shape: "<quest id> <reason>")

```
python3 .court/engine/cli.py advance <id> WORKING --note "<reason>"
```
Then decide whether the existing Serf can address the reason with a
follow-up prompt, or whether a fresh Serf is warranted (same worktree,
never a new one — see the Steward's protocol for dismissing/replacing a
Serf). Communicate the specific reason clearly to whichever Serf continues
the work — do not just bounce the stage without context.
