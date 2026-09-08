---
description: Aggregate provable deliverables, git commits, touched files, endpoints, test exit codes, and production verification runbooks (The Tally)
agent: steward
---
Arguments: $ARGUMENTS

Follow the Coffers Protocol:
1. Run `python3 -m court.cli rollup --section tribute $ARGUMENTS` to extract all rendered Tribute sections.
2. Synthesize the deliverables into a structured treasury inventory:
   - **Git Commits & Hashes**
   - **Files Created & Modified (with line counts)**
   - **Test Suites Executed & Pass Status**
   - **Endpoints, Migrations & Data Artifacts Produced**
   - **The Tally (Production & UI Verification Runbook)**: Specific URLs, UI paths, query parameters, example commands, and expected outcomes to test and verify the deliverables on production.
3. Present the provable ledger of shipped value and verification guide to M'Lord.
