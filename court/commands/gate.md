---
description: Dispatch Gatekeeper to pack Cog Ship, execute batch integration tests on gatehouse, isolate/reject errors with Serf remediation, and merge into castle
agent: steward
---
Quest(s): $ARGUMENTS

Follow `.kilo/prompts/steward.md` §"7. Collect & Gate (`/collect`, `/gate`)":
1. Confirm candidate Quest(s) have passed Master of Coin review and sit at `GATE`
   (`python3 .court/engine/cli.py list --status GATE`).
2. Dispatch/prompt the **Gatekeeper** inside the next available Gatehouse Station worktree session
   (`.kilo/worktrees/the-gatehouse-<station>`, rolling **North → South → East → West → North...**) using `.court/templates/gatekeeper_review_prompt.md`.
   **NEVER run Gatekeeper as a background task, background process, or on castle.**
   Gatekeeper is a **Claude Sonnet Latest** class agent (`openrouter/anthropic/claude-sonnet-latest`).
   Allowed to spawn sequential non-background tasks (`background: false`) for integration verification.
3. **Gatekeeper Protocol Execution**:
   - Gatekeeper decides the **Cog Ship convoy batch** to pack from `GATE`.
   - Gatekeeper merges the candidate branches into the assigned station (`the-gatehouse/<station>`) and executes the unified integration test suite all at once.
   - **Fault Isolation & Rejection**: If tests fail, Gatekeeper isolates/re-tests which specific commit or Quest caused the failure, rejects the offending Quest back to `WORKING`, and dispatches/prompts a Serf in that Quest's worktree with the failure traceback (`.court/templates/serf_remediation_prompt.md`).
   - **Promotion**: For passing Quests, Gatekeeper promotes the verified station branch **directly into `castle`**, compiles the Cog Ship manifest (`python3 .court/engine/cli.py ship`), advances Quests to `READY_FOR_TEARDOWN`, and moves worktrees to **Ashes**.
4. Record Gatekeeper session metadata:
   `python3 .court/engine/cli.py set-field <id> gatekeeper_session_id <session_id>`
   and `python3 .court/engine/cli.py set-field <id> gatekeeper_model "openrouter/anthropic/claude-sonnet-latest"`.

"Ship it" means this normal Gate progression — never skip REVIEW or GATE regardless of phrasing.
