---
description: Commission a new Quest into Agent Manager (worktree, tree branch, section lane, dispatch Serf)
agent: steward
---
Quest: $ARGUMENTS

Follow the Charter Protocol in `.kilo/prompts/steward.md` §"2. Charter & Commission":
1. Look up the Quest record (`court show <id>`).
2. Generate its standardized tree branch (`quest.branch`, e.g., `quest/q001-app-concern`).
3. Start a new Agent Manager worktree session (`agent_manager` start, `mode: worktree`) using `.court/templates/serf_dispatch_prompt.md`, fully filled in.
4. Move the worktree into the matching Agent Manager section (`Bug fix`, `Feature`, `Optimization`, or `Investigation`).
5. Record `branch`, `worktree`, `serf_session_id`, `serf_model` on the Quest via `court set-field`.
6. Advance `court advance <id> DISPATCHED` then `court advance <id> WORKING`.
7. Check/fast-forward the worktree if needed against current staging tip.
