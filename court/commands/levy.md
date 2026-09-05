---
description: Inspect the Court for idle Serfs and dispatch the Master of Coin to audit rendered Tributes
agent: steward
---
Arguments: $ARGUMENTS

Follow the Levy Protocol:
1. **Scan the Court:**
   - Run `python3 .court/engine/cli.py status` to identify Quests in `WORKING` or `DISPATCHED`.
   - Call `agent_manager` (`action: "list"`) to cross-reference each Quest's Serf session state (`idle`, `busy`, `waiting`, `retry`).

2. **Triage Idle Serfs:**
   - **Case A: Serf is `idle` with un-ingested work product / working tree changes:**
     - Prompt the Serf to Bear Tribute using `.court/templates/bear_tribute_prompt.md`.
   - **Case B: Serf has rendered a complete Tribute in `# Tribute Rendered` (or completed report):**
     - Advance the Quest to `REVIEW`:
       ```
       python3 .court/engine/cli.py advance <id> REVIEW --note "Levied: Tribute submitted to Master of Coin"
       ```
     - Dispatch the **Master of Coin** (`.court/templates/master_of_coin_review_prompt.md`, default model **Gemini 3.7 Flash**) to audit the Tribute directly on the Quest's worktree for value, criteria satisfaction, and database compute costs (`RULES.md`).
     - Record:
       ```
       python3 .court/engine/cli.py set-field <id> master_of_coin_session_id <session_id>
       python3 .court/engine/cli.py set-field <id> master_of_coin_model "<model>"
       ```
   - **Case C: Serf is actively `busy`:**
     - Leave in `WORKING` to continue uninterrupted execution.
   - **Case D: Serf is stalled / confused / blocked on Audience:**
     - Triage according to `.kilo/prompts/steward.md` (dismiss + fresh Serf, or raise Audience).

3. **Report to M'Lord:**
   - Return a compressed table/summary of all scanned Serfs, showing:
     - 🪙 **Levied & Sent to Master of Coin** (`REVIEW`)
     - 🔨 **Active Serfs Working** (`WORKING`)
     - 🔴 **Blocked / Pending Audience** (`HELD`)
