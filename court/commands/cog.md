---
description: Assemble the Cog Ship tribute convoy entering the castle, run all rollups (/bard, /atone, /coffers, /murmur), and summarize the deployment into main
agent: steward
---
Arguments: $ARGUMENTS

Follow the Cog Ship Protocol:
> **The Cog Ship**: The convoy of tribute entering the castle. As newly merged Quests accumulate on `castle`, `/cog ship` (or `/ship`) generates a comprehensive deployment manifest combining the Bard's narrative chronicle, the Coffers treasury ledger, the Serf penance backlog, and the Humble Opinions, alongside the git diff/commit vector preparing for promotion from `castle` into `main`.

1. **Reconstruct the Incoming Fleet Manifest:**
   - Execute the deterministic deployment rollup:
     ```bash
     court ship $ARGUMENTS
     ```
   - If filtering by specific Epic or App:
     ```bash
     court ship --epic <id>
     court ship --app <app>
     ```

2. **Inspect the Git Promotion Vector (`castle` -> `main`):**
   - The `court ship` output already includes the ahead/behind counts, commit log, and diffstat for `main..castle`. Review it directly.

3. **Synthesize the 4 Rollup Pillars for M'Lord:**
   - 📜 **The Bard's Chronicle (`/bard`)**: Executive story of challenges, architecture decisions, and transformations shipped.
   - 💰 **The Coffers Ledger (`/coffers`)**: Provable inventory of commits, files created/modified, passed test suites, migrations, endpoints, and data artifacts.
   - ⚖️ **The Serf Penance (`/atone`)**: Actionable technical debt, shortcuts taken, and system/prompt improvement opportunities.
   - 💡 **The Humble Opinions (`/murmur`)**: Ground-level insights and proposed future Quests.

4. **Present the Cog Ship Voyage Report & Readiness Checklist:**
   - Present the structured deployment summary to M'Lord.
   - If M'Lord approves promotion from `castle` to `main`, facilitate the clean merge / release tag.
   - `/cog ship` is read-only reporting — it never merges or advances Quest status itself.
