---
description: Display the current Court dashboard and pipeline overview
agent: steward
---
Arguments: $ARGUMENTS

Run `court status` and cross-reference with active Agent Manager sessions.
Present a concise overview:
- 🔴 Pending Audiences (if any)
- 🟡 In REVIEW / GATE (Master of Coin or Gatekeeper active)
- 🔵 WORKING (active Serfs in worktrees)
- ⚪ READY_FOR_TEARDOWN (the prune queue for M'Lord)
