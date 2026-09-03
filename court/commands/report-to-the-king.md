---
description: Demand that a Serf (or all active Serfs) Bear Tribute and submit their Report to the King
agent: steward
---
Target Quest: $ARGUMENTS

Follow the Steward's Bear Tribute protocol:

1. Identify the target Quest(s):
   - If a specific Quest ID or branch is provided in `$ARGUMENTS`, target that Quest.
   - If no arguments are provided, target all active Quests currently in `WORKING` stage with an active Serf session (`python3 .court/engine/cli.py status`).

2. For each target Quest:
   - Load Quest details: `python3 .court/engine/cli.py show <id>`.
   - Retrieve the Serf's session ID (`serf_session_id`) and verify it in `agent_manager list`.
   - Dispatch the Bear Tribute prompt (`.court/templates/bear_tribute_prompt.md`) to the Serf session via `agent_manager` (`action: "prompt"`).
   - Ingest the Serf's response and extract the five required sections:
     - **Ballad**: Narrative summary of work and decisions.
     - **Tribute**: Provable work product (files touched, git status/commits, tests run).
     - **Penance**: Agent self-flagellation on gaps, shortcuts, or low confidence.
     - **Audience**: Decision requests for M'Lord.
     - **Humble Opinion**: Recommended next steps.
   - Record the rendered Tribute into the Quest file:
     ```
     python3 .court/engine/cli.py set-section <id> "Tribute Rendered" --file <report_temp_file>
     ```
   - If the Serf requested an Audience, record it in `.court/LEDGER.md` and the Quest's "Audience Log".

3. Present a synthesized Bear Tribute dashboard to M'Lord.
