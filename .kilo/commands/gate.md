---
description: Dispatch Gatekeeper to run test suite on gatehouse layer and merge into castle
agent: steward
---
Quest: $ARGUMENTS

Follow the Gate Protocol in `.kilo/prompts/steward.md` §"5. Gatekeeper (GATE -> READY_FOR_TEARDOWN)":
1. Confirm the Quest has passed Master of Coin review and sits at `GATE` (`court show <id>`).
2. Dispatch the **Gatekeeper** inside the persistent `gatehouse` worktree using `.court/templates/gatekeeper_review_prompt.md`.
   **NEVER run Gatekeeper as a background task or on castle.** Process candidates **strictly one per pull / merge**, sequentially.
3. Record `gatekeeper_session_id` and `gatekeeper_model`.
4. Confirm the Gatekeeper actually merged (check the Quest's `# Gatekeeper Review` section for a real merge commit hash). If confirmed:
   ```bash
   court advance <id> READY_FOR_TEARDOWN --note "Merged into castle; queued for teardown in Ashes"
   ```
5. Move the worktree to Agent Manager's **Ashes** section as a visual cue.

"Ship it" means this normal Gate progression — never skip REVIEW or GATE regardless of phrasing.
