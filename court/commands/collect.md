---
description: Collect approved Tributes from Master of Coin, dispatch Gatekeeper for final test execution on gatehouse, and merge into castle
agent: steward
---
Arguments: $ARGUMENTS

Follow the Collect Protocol:
> **Steward Principle on Collect Upward**: The Steward does NOT burden himself with deep code inspection, diff auditing, or test execution. The Steward's role is orchestration, valuation routing, and reading durable completions. If the Steward is suspicious or uncertain of a report, he routes it to the Gatekeeper for independent verification.

1. **Audit Quests at `REVIEW`:**
   - Run `court list --status REVIEW` to identify Quests under Master of Coin evaluation.
   - Inspect `# Master of Coin Review` for each Quest.

2. **Route by Valuation Verdict:**
   - **Case A: Value Approved (Tribute Acceptable):**
     - Advance to `GATE`:
       ```bash
       court advance <id> GATE --note "Collected: Master of Coin approved value; dispatched to Gatekeeper"
       ```
     - Dispatch the **Gatekeeper** inside the persistent `gatehouse` worktree (`.kilo/worktrees/integration` or `.kilo/worktrees/gatehouse`) using `.court/templates/gatekeeper_review_prompt.md`.
     **NEVER run Gatekeeper in a background task or on castle.** Process candidates **strictly one per pull / merge**, sequentially.
     - Record `gatekeeper_session_id` and `gatekeeper_model`.
   - **Case B: Value Deficient / Incomplete / Wasteful:**
     - Return the Quest to `WORKING`:
       ```bash
       court advance <id> WORKING --note "Master of Coin rejected: <one-line reason>"
       ```

3. **Gatekeeper Execution & Automatic Merge to `castle`:**
   - The Gatekeeper independently executes the test suite on the `gatehouse` layer.
   - When tests pass, the Gatekeeper merges the Quest's branch into `gatehouse`, verifies integration, merges/promotes into `castle`, and records the merge commit hash into `# Gatekeeper Review`.
   - Advance the Quest to `READY_FOR_TEARDOWN`:
     ```bash
     court advance <id> READY_FOR_TEARDOWN --note "Gatekeeper merged into castle (<commit_hash>); queued for teardown"
     ```
   - Move the worktree to Agent Manager's **Ashes** section as a visual cue.

4. **Report to M'Lord (Cog Ship Deployment Convoy Summary):**
   - Run the Cog Ship rollup (`/cog ship`) to combine the Bard/Coffers/Atone/Murmur
     rollups for every Quest just merged into `castle`:
     ```bash
     court ship
     ```
   - Summarize the pipeline status:
     - 🏰 **Merged into Castle** (`READY_FOR_TEARDOWN`) + Cog Ship convoy summary
     - 🛡️ **Under Gatekeeper Testing on Gatehouse** (`GATE`)
     - ↩️ **Returned to Working** (`WORKING`)
