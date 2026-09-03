---
description: Dispatch a Serf into an isolated worktree for a Chartered (PLANNED) Quest
agent: steward
---
Quest: $ARGUMENTS

Follow `.kilo/prompts/steward.md` §"5. Dispatch":
1. Load the Quest: `python3 .court/engine/cli.py show <id>`. It must be `PLANNED`
   (Chartered via `/charter` or `/plot`'s Council) with a Goal & Scope and Expected
   Tribute already filled in — if not, stop and redirect to `/quest`/`/charter` first.
2. Start a new Agent Manager worktree session (`agent_manager` `start`,
   `mode: worktree`) using the filled-in `.court/templates/serf_dispatch_prompt.md`.
   Ensure `branchName` strictly follows the organizational folder hierarchy with slashes
   (`quest/<quest_id>-<slug>` or `quest/<epic_id>/<quest_id>-<slug>`, NEVER flat hyphens like `quest-...`).
   Default model: Gemini 3.7 Flash, unless this Quest warrants a smarter Serf.
3. Record the result:
   ```
   python3 .court/engine/cli.py set-field <id> branch <branch>
   python3 .court/engine/cli.py set-field <id> worktree <worktree_path>
   python3 .court/engine/cli.py set-field <id> serf_session_id <session_id>
   python3 .court/engine/cli.py set-field <id> serf_model "<model actually used>"
   python3 .court/engine/cli.py advance <id> DISPATCHED --note "Serf dispatched"
   ```
4. Check the known Agent Manager base-branch staleness issue from
   `AGENTS.md` before trusting the worktree is current.
