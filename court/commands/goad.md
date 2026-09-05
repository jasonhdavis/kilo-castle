---
description: Nudge an idle or exited Serf session back to life, or diagnose whether it actually finished
agent: steward
---
Quest: $ARGUMENTS

Follow the Goad Protocol:

1. **Check first whether an agent is even needed (zero-agent-turn precondition):**
   Run `python3 .court/engine/cli.py rebase <id> --dry-run`, then
   `python3 .court/engine/cli.py rebase <id>` if it shows drift. This mechanically runs
   `git merge castle` with zero agent turns. If it reports `merged` or
   `already_up_to_date`, that alone is **not** the goad — drift removal is a
   precondition, not a substitute. Continue to step 2 regardless, unless the rebase
   reports a real `conflict` that blocks everything until a Serf resolves it by hand.

2. Call `agent_manager` (`action: "list"`) to check whether a session still exists for
   this Quest's worktree/branch (`python3 .court/engine/cli.py show <id>` gives the
   `branch:`/`worktree:` fields if you need to match by branch name).

3. **The goad prompt is mandatory — never skip it, and never substitute "no drift" or
   "tasks are checked off" for actually sending it.** Determine which situation you're in:
   - **A session exists for that worktree** (shows up in `list`, `idle` or `busy`): send
     it `.court/templates/goad_prompt.md`, filling in `<QUEST_ID>` and
     `<canonical_branch>`, via `agent_manager` (`action: "prompt"`) with that
     `sessionID`. Prompts to busy sessions queue automatically — you do not need to wait
     for idle.
   - **No session exists for that worktree at all** (the worktree/branch appears in
     `list` but has no `session`/`sessions` field — this happens routinely once a Serf's
     terminal exits after finishing its checklist, and does **not** mean the Quest is
     done; see `.court/templates/goad_prompt.md` Case A): start a brand-new Agent
     Manager session bound to that exact existing branch/worktree (`mode: "worktree"`,
     `branchName` set to the Quest's canonical branch) with `prompt` set to the full,
     filled-in contents of `.court/templates/goad_prompt.md` as the initial prompt.

4. **Verify the goad actually landed** before reporting anything to M'Lord: re-run
   `agent_manager` (`action: "list"`) and confirm the targeted worktree now shows a
   session (`busy`, or freshly `idle` after replying), or re-run
   `python3 .court/engine/cli.py show <id>` after the session responds and confirm
   either `# Tribute Rendered` is now filled in with `status: REVIEW`, or the task
   checklist moved forward. A goad that produced no visible session and no ledger
   change did not happen — do not log success for it.

5. Log the action (only after step 4 confirms it actually happened):
   `python3 .court/engine/cli.py log <id> "Goaded idle working session"` — if you had to
   start a fresh session because the old one had exited, say so explicitly in the note.
