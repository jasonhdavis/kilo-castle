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
     court new --app <app> --concern <concern> --title "<title>" --section "Investigation" --kind "scout" --goal "<goal>"
     ```
2. **Charter the Scout:**
   - Branch format: `scout/<id>-<slug>`.
   - Dispatch an Agent Manager worktree session using `.court/templates/scout_dispatch_prompt.md`.
   - Move worktree to the `Investigation` section in Agent Manager.
   - Record fields via `court set-field` and advance to `WORKING`.
3. **Scout Constraints & Deliverables:**
   - Scout writes throwaway scripts and fixtures to `tasks/artifacts/` (NO production service code).
   - Scout renders the 5-part Scout Report (**The Survey**, **The Map**, **The Dangers**, **The Tribute**, **The Plot**).
4. **Transition to Production:**
   - Once the Scout Report is rendered, M'Lord and Steward review via `/plot` to design production Quests.
   - The `scout/*` branch is moved to `Ashes` and never auto-merged wholesale into staging.
