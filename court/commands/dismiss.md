---
description: Dismiss a stalled/confused Serf and dispatch a fresh one into the same worktree
agent: steward
---
Target: $ARGUMENTS (a Quest id, or directly a session id)

Follow `.kilo/prompts/steward.md` §"3. Monitor (WORKING)"'s dismissal steps
exactly:
1. Clean up the stalled session via Kilo CLI (`kilo session delete <sessionID>`;
   worktree/branch survive this untouched).
2. Check `kilo session list` for that worktree — it may already carry another
   usable session.
3. If no usable session remains, dispatch a fresh one yourself via Kilo CLI
   (`court goad <id>`, or `kilo run --agent serf --model "GLM-5.3-Flash" --provider openrouter --dir <wt>`).
   Do NOT create a second worktree/branch as a workaround — that forks the
   Quest's implementation.
4. Log it: `python3 -m court.cli log <quest_id> "Dismissed serf X, dispatched fresh serf Y"`
   and add a one-line entry to `.court/LEDGER.md`'s "Serf/Vassal churn log".
5. When dispatching a fresh Serf session, always ensure it is started with GLM 5.3 Flash (`model: "GLM-5.3-Flash"`, `provider: "openrouter"`).
