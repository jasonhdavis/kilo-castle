---
description: Dismiss a stalled/confused Serf and dispatch a fresh one into the same worktree
agent: steward
---
Target: $ARGUMENTS (a Quest id, or directly a session id)

Follow `.kilo/prompts/steward.md` §"3. Monitor (WORKING)"'s dismissal steps
exactly:
1. `agent_manager` `stop` on the stalled session (worktree/branch survive
   this untouched).
2. Check `agent_manager` `list` for that worktree — it may already carry a
   usable second session slot.
3. If the tool has no way to attach a new session to the existing worktree
   yourself, say so plainly and ask M'lord to open a fresh session tab on
   that worktree in the Agent Manager UI. Do NOT create a second
   worktree/branch as a workaround — that forks the Quest's implementation.
4. Log it: `python3 -m court.cli log <quest_id> "Dismissed serf X, dispatched fresh serf Y"`
   and add a one-line entry to `.court/LEDGER.md`'s "Serf/Vassal churn log".
5. When dispatching a fresh Serf session, always ensure it is started with GLM 5.3 Flash (`model: "GLM-5.3-Flash"`, `provider: "openrouter"`).
