---
description: Confirm a Quest for implementation — one composite charter command, spawn Serf, one composite dispatch-complete command landing at WORKING
agent: steward
---
Quest & Notes: $ARGUMENTS

Follow the Charter Protocol in `.kilo/prompts/steward.md` §"Charter":

1. **Load and Inspect the Quest**:
   - Run `python3 -m court.cli show <id>`. It may be in `OPEN` (newly created via `/quest`).
   - **Out-of-Character Check**: Ensure `# The Kingdom Requires` and `# Expected Tribute` are written **100% out of character** in clean, domain-accurate engineering language with zero Castle/Court roleplay metaphors.

2. **Charter in One Command (fold notes → advance to PLANNED → compute branch)**:
   - The old two-invocation pre-dispatch plumbing (`set-section --append` + `advance PLANNED`) is replaced by the composite command:
     ```bash
     python3 -m court.cli charter <id> --notes "M'Lord's Charter Notes: <notes>" --section "Feature"
     ```
   - `--notes` is optional (omit when M'Lord supplied nothing new); `--section` overrides the Agent Manager lane (defaults to the Quest's existing `section` field). The command is idempotent — a Quest already `PLANNED` or beyond is left in place, and it still folds any `--notes`.
   - The command prints a **NEXT STEPS** block with everything needed for step 3: the canonical `branchName`, the mandatory `model`/`provider` pair, the Serf template path, and the target Agent Manager section.

3. **Dispatch and Stand Up the Serf via Court CLI**:
   - Stand up the Serf worktree and session directly via Court CLI:
     ```bash
     python3 -m court.cli dispatch <id> --standup
     ```
     (or combine both steps with `python3 -m court.cli charter <id> --dispatch`).
   - This directly invokes Kilo's CLI standup (`kilo worktree create` + `kilo run --agent serf` / API), creating the worktree under `.kilo/worktrees/`, configuring `.kilo/kilo.json` with `"default_agent": "serf"`, enforcing `task: deny` permissions, and passing pure charter task instructions without persona prompt corruption.
   - Alternatively, if manually creating an Agent Manager UI session in section `Bug fix`/`Feature`/`Optimization`, record its IDs via `python3 -m court.cli dispatch-complete <id> --session-id <SESSION_ID> --branch <branch> --worktree <worktree_path>`.

4. **Record Metadata and Land at Working in One Command**:
   - The old six-invocation sequence (four `set-field` + two `advance`) is replaced by the composite command, using the real IDs the `agent_manager` tool call returned:
     ```bash
     python3 -m court.cli dispatch-complete <id> --session-id <SESSION_ID> --branch <branch> --worktree <worktree_path>
     ```
   - `--model`/`--serf-model` default to `GLM-5.3-Flash` (aliases for the same field), so the common case needs no extra typing. All six mutations land as one ledger commit. The command prints a NEXT STEPS reminder (the Quest is now `WORKING`; next action is normal Serf toil, then `court levy <id>` once the Serf reports done).

5. **Report to M'Lord**:
   - Confirm the Serf is toiling at Working (rendered with `status_label()` as Working).
   - **Look-ahead**: Note what is ready for `/levy` next.
