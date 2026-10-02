---
description: Dismiss a stalled/confused Serf and dispatch a fresh one into the same worktree
agent: steward
---
Target: $ARGUMENTS (a Quest id, or directly a session id)

Follow `.kilo/prompts/steward.md` §"3. Monitor (WORKING)"'s dismissal steps
exactly:
1. Kill the stalled session's worker process (SIGTERM, then SIGKILL if needed) — find the
   pid via the process table scoped to the Quest's worktree (`ps aux | grep -i kilo run`).
   Worktree/branch survive this untouched. NEVER call `agent_manager` — the tool is
   permission-denied in `kilo.json` for every agent in this project (headless runs hang on
   it until timeout, and Agent Manager cannot see CLI-spawned sessions).
2. Dispatch the fresh Serf into the SAME worktree via the Court CLI:
   `python3 -m court.cli dispatch <quest_id> --standup` (or
   `python3 -m court.cli goad <quest_id>` when the stalled session is still resumable).
   Do NOT create a second worktree/branch as a workaround — that forks the Quest's
   implementation.
3. If no live worker process can be resolved, say so plainly and ask M'lord to open a
   fresh session tab on that worktree in the Agent Manager UI.
4. Log it: `python3 -m court.cli log <quest_id> "Dismissed serf X, dispatched fresh serf Y"`
   and add a one-line entry to `.court/LEDGER.md`'s "Serf/Vassal churn log".
5. When dispatching a fresh Serf session, always ensure it is started with GLM 5.3 Flash (`model: "GLM-5.3-Flash"`, `provider: "openrouter"`).
