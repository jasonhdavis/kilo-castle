---
description: Collect approved Tributes from Master of Coin, dispatch Gatekeeper for final test execution on gatehouse, and merge into castle
agent: steward
---
Arguments: $ARGUMENTS

Follow the Collect Protocol:
> **Steward Principle on Collect Upward**: The Steward does NOT burden himself with deep code inspection, diff auditing, or test execution. The Steward's role is orchestration, valuation routing, and reading durable completions. If the Steward is suspicious or uncertain of a report, he routes it to the Gatekeeper for independent verification.

1. **Audit Quests at `REVIEW`:**
   - Run `python3 .court/engine/cli.py list --status REVIEW` to identify Quests under Master of Coin evaluation.
   - Inspect `# Master of Coin Review` for each Quest (or await Master of Coin session completion).

2. **Route by Valuation Verdict:**
   - **Case A: Value Approved (Tribute Acceptable):**
     - Advance to `GATE`:
       ```
       python3 .court/engine/cli.py advance <id> GATE --note "Collected: Master of Coin approved value; dispatched to Gatekeeper"
       ```
      - Dispatch the **Gatekeeper** inside the next available Gatehouse Station worktree (`.kilo/worktrees/the-gatehouse-<station>`, rolling **North → South → East → West → North...**) using `.court/templates/gatekeeper_review_prompt.md`.
         **NEVER run Gatekeeper in a background task, background process, or on castle.**
         Gatekeeper is a **Claude Sonnet Latest** class agent (`openrouter/anthropic/claude-sonnet-latest`).
         Allowed to spawn sequential non-background tasks (`background: false`) for integration verification.
      - Record:
        ```
        python3 .court/engine/cli.py set-field <id> gatekeeper_session_id <session_id>
        python3 .court/engine/cli.py set-field <id> gatekeeper_model "openrouter/anthropic/claude-sonnet-latest"
        ```
    - **Case B: Value Deficient / Incomplete / Wasteful:**
      - Return the Quest to `WORKING`:
        ```
        python3 .court/engine/cli.py advance <id> WORKING --note "Master of Coin rejected: <one-line reason>"
        ```

3. **Gatekeeper Execution, Cog Ship Packing & Direct Promotion to `castle`:**
   - The Gatekeeper surveys candidate Quests at `GATE` and decides the **Cog Ship convoy batch** to pack.
   - The Gatekeeper merges the pack into the assigned station (`the-gatehouse/<station>`) and executes the unified integration test suite all at once.
   - **Fault Isolation & Serf Remediation**: If tests fail, the Gatekeeper isolates/re-tests which commit/Quest failed, rejects the offending Quest back to `WORKING`, and dispatches/prompts a Serf in that Quest's worktree with the failure traceback (`.court/templates/serf_remediation_prompt.md`).
   - **Promotion**: When tests pass, the Gatekeeper promotes the verified station branch **directly into `castle`**, packs the Cog Ship manifest (`python3 .court/engine/cli.py ship`), advances Quests to `READY_FOR_TEARDOWN`, and moves worktrees to **Ashes**.

4. **Report to M'Lord (Cog Ship Deployment Convoy Summary):**
   - Run the full Cog Ship rollup (`/cog ship`, per the Steward's §"8. Cog Ship" protocol)
     to combine the Bard, Coffers, Atone, and Murmur rollups for every Quest just merged
     into `castle`:
     ```
     python3 .court/engine/cli.py ship
     ```
   - Summarize the pipeline status:
     - 🏰 **Merged into Castle** (`READY_FOR_TEARDOWN`) + Cog Ship convoy summary (Bard/Coffers/Atone/Murmur)
     - 🛡️ **Under Gatekeeper Testing on Gatehouse** (`GATE`)
     - ↩️ **Returned to Working** (`WORKING`)
