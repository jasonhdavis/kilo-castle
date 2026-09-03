---
description: Aggregate provable deliverables, git commits, touched files, endpoints, and test exit codes across Quests
agent: steward
---
Arguments: $ARGUMENTS

Follow the Coffers Protocol:
1. Run `court rollup --section tribute $ARGUMENTS` to extract all rendered Tribute sections.
2. Synthesize the deliverables into a structured treasury inventory:
   - **Git Commits & Hashes**
   - **Files Created & Modified (with line counts)**
   - **Test Suites Executed & Pass Status**
   - **Endpoints, Migrations & Data Artifacts Produced**
3. Present the provable ledger of shipped value to M'Lord.
