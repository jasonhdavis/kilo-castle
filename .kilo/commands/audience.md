---
description: List pending Audience decisions, or interrogate a specific Quest's worktree with a live, Master-of-Coin-verified question
agent: steward
---
Arguments: $ARGUMENTS

Two modes, dispatched on the shape of `$ARGUMENTS`:

## Mode A — No arguments (or arguments that aren't `<quest_id> <question>`): List Pending Audiences

Scan `.court/quests/*.md` and `.court/epics/*.md` for status `HELD` and any
"Audience Log" sections with unresolved entries, plus `.court/LEDGER.md`'s
"Audience log (compressed)" section for anything not yet closed out.

Present each pending Audience in the required shape: the decision required,
concise context, available options, your recommendation, and consequences.
If there are none, say so plainly — do not manufacture a decision just to
have something to show.

## Mode B — `<quest_id> "<question>"`: Interrogate a Worktree

When `$ARGUMENTS` starts with a Quest/Epic ID (`Q\d+`) followed by a question, M'Lord
is putting a live question to that specific Quest's worktree instead of listing pending
decisions. This is answered by the **Master of Coin**, who must first "make sure the
books are in order" (re-verify live state — `git status`, drift, and whatever the
question itself turns on) before answering, exactly as documented in
`.court/templates/master_of_coin_interrogate_prompt.md`. This never renders a
Pass/Fail verdict, never pilliories, and never touches the diff — it is read-only
interrogation, logged as a dated entry in that Quest's own `## Audience Log`.

1. **Load the Quest's coordinates**: `python3 -m court.cli show <quest_id>` for
   `branch`, `worktree`, and `master_of_coin_session_id`.
2. **Reuse an existing Master of Coin session if one is live** for this Quest:
   `agent_manager` `action: "list"` — if `master_of_coin_session_id` shows up there
   (any state except `offline`/missing), send it the filled interrogation prompt via
   `agent_manager` `action: "prompt"`, `sessionID: "<master_of_coin_session_id>"`.
3. **Otherwise spawn a fresh, dedicated session** bound to the Quest's exact existing
   branch — `agent_manager` `start`, `mode: "worktree"`, `branchName: "<branch>"`,
   `model: "Gemini 3.8 Flash"`, `provider: "openrouter"` — with the filled
   `.court/templates/master_of_coin_interrogate_prompt.md` as its initial prompt.
   **Never** hijack the Serf's own `serf_session_id`, and **never** run this as a
   `task` subagent on `castle`. Record the new session so future interrogations (and
   `/levy`) can reuse it:
   ```bash
   python3 -m court.cli set-field <quest_id> master_of_coin_session_id <session_id>
   python3 -m court.cli set-field <quest_id> master_of_coin_model "openrouter/google/gemini-3.8-flash"
   ```
4. **Relay the verified answer to M'Lord** once the session responds, including what
   was verified (not just the conclusion). The answer is already durable — the Master
   of Coin appends it to `## Audience Log` in `.court/quests/<quest_id>.md` itself, so
   nothing further needs to be written back by the Steward.
