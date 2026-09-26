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

5. **Multiple UI Quests Pending? Combine Into a Studio or Atelier Instead**:
   - If several UI-review-pending Quests are stacked up, do NOT spawn one artist session per Quest — merge them into ONE reviewed state with ONE artist:
     - `/studio <QID1>,<QID2>,...` (`court studio`) — the **deterministic Combined Studio** (Q-2): cuts an `artist-studio-<ids>` worktree from the castle tip, merges the N quest branches with the established conflict policy (charter paperwork → branch-wins; genuine code overlap → disclosed union), boots the freshness-gated runserver, and records the studio + session in the ledger. Per-quest sync-back after sign-off; collection stays with the Steward.
     - `/atelier <QID1>,<QID2>,...` (`court atelier`) — the **pre-collect Cog Ship convoy** variant: stamps the convoy and advances the quests to GATE; the Gatekeeper promotes the convoy branch into castle after the unified suite.
   - The spawn protocol above (Branch A: Agent Manager; Branch B: CLI fallback) applies unchanged to the studio/atelier artist session — record its session id on every studio/atelier quest.
   - The per-Quest `/artist` flow above remains the right tool for a single Quest.
