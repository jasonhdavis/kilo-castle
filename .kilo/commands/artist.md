---
description: Spawn a dedicated Court Artist session with runserver for UI review
agent: steward
---
Arguments: $ARGUMENTS

Follow the Court Artist Protocol:

1. **Parse Arguments**:
   - Extract `<quest_id>` from `$ARGUMENTS` (e.g. `Q196`).
   - Extract optional `--model <model>` or custom model string if provided by M'Lord (Court Artist supports flexible model selection; default is `openrouter/z-ai/glm-5.3`, matching the other personas' qualified `provider/model` convention).

2. **Execute Deterministic Artist CLI Command**:
   - Run:
     ```bash
     python3 -m court.cli artist <quest_id> [--model "<model>"] --json
     ```
   - This deterministically:
     - Resolves the Quest's worktree path and branch.
     - Starts or verifies the worktree development server on its dedicated port via `.kilo/manage_servers.sh start <worktree>`.
     - Extracts target preview routes from the Quest's Tally and touched templates.
     - Renders `.court/templates/court_artist_prompt.md` with live URLs, design system instructions, and authority boundaries.
     - Logs the summons in the Quest's Castle Ledger and records `artist_model`.

3. **Check or Spawn Dedicated Court Artist Session**:
   - Query `agent_manager` (`action: "list"`).
   - If `artist_session_id` is already recorded on the Quest and is active in `agent_manager`:
     - Prompt that existing session with M'Lord's directive via `agent_manager` `action: "prompt"`, `sessionID: "<artist_session_id>"`.
   - If no active session exists:
     - Spawn a **brand-new, dedicated** Agent Manager session bound to the Quest's existing branch:
       - Call `agent_manager` with:
         - `mode: "worktree"`
         - `tasks`:
           - `name`: `"<short_id> Court Artist"`
           - `branchName`: `"<branch>"`
           - `model`: `"<model>"`
           - `provider`: `"<provider>"`
           - `prompt`: `"<rendered_prompt>"`
     - Record the new session ID on the Quest:
       ```bash
       python3 -m court.cli set-field <quest_id> artist_session_id <session_id>
       python3 -m court.cli set-field <quest_id> artist_model "<model>"
       ```

4. **Present the Royal Studio Easel to M'Lord**:
   - Report the active Court Artist session name and ID.
   - Display the live preview URL (`http://localhost:<port>/<route>`).
   - Note the interaction protocol:
     - M'Lord provides visual direction directly to the Court Artist.
     - The Court Artist makes live template/view edits and prompts M'Lord to refresh the browser.
     - Once satisfied, the Court Artist commits the artwork, updates the Tally, and prepares the Quest for Master of Coin audit and Gatehouse collection.
