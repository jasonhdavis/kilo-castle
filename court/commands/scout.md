---
description: Dispatch an exploratory Scout agent to pioneer methods, probe APIs, and generate a 5-part Scout Report
agent: steward
---
Arguments: $ARGUMENTS

Follow the Scout Reconnaissance Protocol:
1. **Intake & Scope:**
   - Define the exploratory goal, target API, or data-science hypothesis.
   - Assign to section `Investigation` and kind `scout`.
   - Create Quest:
     ```bash
     python3 -m court.cli new --app <app> --concern <concern> --title "<title>" --section "Investigation" --kind "scout" --goal "<goal>"
     ```
2. **Charter the Scout:**
   - Branch format: `scout/<id>-<slug>`.
   - Dispatch an Agent Manager worktree session using `.court/templates/scout_dispatch_prompt.md`.
   - Move worktree to the `Investigation` section in Agent Manager.
   - Record fields via `python3 -m court.cli set-field` and advance to `WORKING`.
3. **Scout Constraints & Deliverables:**
   - Scout writes throwaway scripts and fixtures to `tasks/artifacts/` (NO production service code).
   - Scout renders the 5-part Scout Report (**The Survey**, **The Map**, **The Dangers**, **The Tribute**, **The Plot**).
4. **Transition to Production:**
   - Once the Scout Report is rendered, M'Lord and Steward review via `/plot` to design production Quests.
   - **Automatic close-out**: when `/plot`'s Confirmation Gate seals a Plot sourced from this
     Scout's report, the Scout Quest is advanced to `READY_TO_RAZE` and queued for Ashes as a
     natural side effect of sealing — never a separate manual step (see `plot.md` §"Scout Close-Out").
   - The `scout/*` branch is moved to `Ashes` and never auto-merged wholesale into staging.
