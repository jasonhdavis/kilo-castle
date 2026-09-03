---
description: Queue a completed or obsolete Quest worktree for teardown and move it into the Ashes section
agent: steward
---
Quest: $ARGUMENTS

Follow the Raze Protocol:
1. **Target Identification:**
   - If a Quest ID is provided ($ARGUMENTS), inspect with `python3 .court/engine/cli.py show <id>`.
   - If no arguments provided, run `python3 .court/engine/cli.py teardown-list` and scan `agent_manager` (`action: "list"`).

2. **Raze & Move to Ashes:**
   - Advance Quest status:
     ```
     python3 .court/engine/cli.py advance <id> READY_FOR_TEARDOWN --note "Razed: queued for teardown and moved to Ashes"
     ```
   - Find the **Ashes** section in `agent_manager` (`action: "list"`).
   - Move the worktree session to the **Ashes** section:
     `agent_manager` with `{ "action": "move", "sessionID": "<session_id>", "sectionID": "<ashes_section_id>" }`.
   - Never delete or stop the worktree directory directly — worktree removal is always M'Lord's manual action in the Agent Manager UI.

3. **Report to M'Lord:**
   - Display the list of all worktrees resting in the **Ashes** section awaiting M'Lord's final manual deletion.
