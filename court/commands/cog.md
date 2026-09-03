---
description: Assemble the Cog Ship tribute convoy entering the castle, run all rollups (/bard, /atone, /coffers, /murmur), and summarize the deployment into main
agent: steward
---
Arguments: $ARGUMENTS

Follow the Cog Ship Protocol:
> **The Cog Ship**: The convoy of tribute entering the castle, built and packed by the **Gatekeeper** as verified Quests pass through the gatehouse. As newly merged Quests accumulate on `castle`, `/cog ship` (or `/ship`) generates a comprehensive deployment manifest combining the Bard's narrative chronicle, the Coffers treasury ledger, the Serf penance backlog, and git diff/commit vectors preparing for promotion from `castle` into `main`.

1. **Reconstruct the Incoming Fleet Manifest:**
   - Execute the deterministic deployment rollup:
     ```bash
     python3 .court/engine/cli.py ship $ARGUMENTS
     ```
   - If filtering by specific Epic or App:
     ```bash
     python3 .court/engine/cli.py ship --epic <id>
     python3 .court/engine/cli.py ship --app <app>
     ```

2. **Inspect the Git Promotion Vector (`castle` -> `main`):**
   - Review pending commits on `castle` ahead of `main` (`git log main..castle --oneline`).
   - Review aggregate file changes (`git diff --stat main..castle`).

3. **Synthesize the 4 Rollup Pillars for M'Lord:**
   - 📜 **The Bard's Chronicle (`/bard`)**: Executive story of challenges, architecture decisions, and transformations shipped.
   - 💰 **The Coffers Ledger (`/coffers`)**: Provable inventory of commits, files created/modified, passed test suites, migrations, endpoints, and data artifacts.
   - ⚖️ **The Serf Penance (`/atone`)**: Actionable technical debt, shortcuts taken, and system/prompt improvement opportunities.
   - 💡 **The Humble Opinions (`/murmur`)**: Ground-level insights and proposed future Quests.

4. **Present the Cog Ship Voyage Report & Readiness Checklist:**
   - Present the structured deployment summary to M'Lord with confidence score.
   - If M'Lord approves promotion from `castle` to `main`, facilitate the clean merge / release tag.
