---
description: Blueprint upcoming work, consult on strategic priorities, and prepare candidate Quests
agent: steward
---
Arguments: $ARGUMENTS

Follow the Plot Protocol:
1. **Reconstruct Strategic Context:**
   - Read Royal Edicts & Decrees (`court edict` / `.court/EDICTS.md`).
   - Read current in-flight and open Quests (`court status` / `court list --status OPEN,WORKING`).
   - Read active planning files (`tasks/ACTIVE.md`, `tasks/apps/`, `planning/`).
   - Read project backlog (`tasks/BACKLOG.md`).
   - Extract recent Serf field intelligence via `court rollup --section opinion` and `court rollup --section penance`.

2. **Synthesize & Blueprint Candidate Quests:**
   - Group candidate work into clear domains / concerns.
   - For each candidate: define Goal & Scope, section lane (`Bug fix`, `Feature`, `Optimization`, `Investigation`), and expected acceptance criteria.
   - Highlight connections: "You decreed X, and during Quest Y the serfs uncovered Z."

3. **Question-Forward Consultation:**
   - Present a concise roadmap overview with numbered options.
   - Proactively ask M'Lord structured questions to clarify priority, sequencing, and trade-offs.
   - Await M'Lord's direction before charting the chosen Quests.
