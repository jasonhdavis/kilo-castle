---
description: Goad an idle Serf session in working phase to update charter and continue
agent: steward
---
Arguments: $ARGUMENTS

1. **Check first whether an agent is even needed (Q149, 2026-09-05):** run `python3 -m court.cli rebase <id> --dry-run`, then `python3 -m court.cli rebase <id>` if it shows drift. This mechanically runs `git merge castle` with zero agent turns, auto-resolving the two routine conflict shapes (foreign Quests' ledger files, and this quest's own History-table-only divergence). If it reports `merged` or `already_up_to_date`, that alone is **not** the goad — drift removal is a precondition, not a substitute. **Do not stop here and report success.** Continue to step 2 regardless, unless the rebase reported a real `conflict` that blocks everything until a Serf resolves it by hand.
2. Call `agent_manager` with `action: "list"` to check every worktree/session tied to this Quest's branch (`court show <id>` gives the `branch:`/`worktree:` fields if you need to match by branch name instead of by name string).
3. **The goad prompt is mandatory — execute via Court CLI / Kilo CLI:**
   - Execute the CLI goad command directly:
     ```bash
     python3 -m court.cli goad <id>
     ```
   - This directly invokes Kilo CLI (`kilo run --agent serf --dir <wt>`) with `.court/templates/goad_prompt.md`, pinning `--agent serf` with GLM 5.3 Flash, logging to `.kilo/serf.log`, and updating the Quest's `serf_session_id`.
   - Bypasses Agent Manager UI to prevent the server-context `steward` inheritance bug.
   - If prompting an existing live interactive session in Agent Manager, use `agent_manager` `action: "prompt"`. Otherwise, `court goad <id>` is the canonical path.
4. **Verify the goad actually landed** before reporting anything to M'Lord: re-run `agent_manager` `action: "list"` and confirm the targeted worktree now shows a session (`busy` or freshly `idle` after replying), or re-run `court show <id>` after the session responds and confirm either `# Tribute Rendered` is now filled in + `status: TRIBUTE_READY`, or the task checklist moved forward. A goad that produced no visible session and no ledger change did not happen — do not log success for it.
5. Log action: `python3 -m court.cli log <id> "Goaded idle working session"` (only after step 4 confirms it actually happened; if you had to start a fresh session because the old one had exited, say so explicitly in the note, e.g. `"Goaded Q151: no live session found, started fresh Serf session in existing worktree"`).
