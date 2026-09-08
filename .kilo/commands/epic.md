---
description: Create a new Epic Quest and dispatch a Vassal to decompose it
agent: steward
---
M'lord's Epic goal: $ARGUMENTS

Only use this for a genuinely large, multi-Quest initiative — not ordinary
work (that's `/quest`). Follow `.court/templates/vassal_dispatch_prompt.md`:

1. Create the Epic record:
   ```
   python3 -m court.cli new --app <app> --concern <concern> \
     --title "<short title>" --kind epic --goal "<Epic goal>"
   ```
   Immediately commit the new Epic file to `castle` (e.g. `git add .court/epics/<epic_id>.md && git commit -m "court: create <epic_id> (<title>)"`).
2. Dispatch a Vassal (Agent Manager `local` mode is fine — it coordinates,
   it doesn't need its own worktree) using the filled-in
   `vassal_dispatch_prompt.md` template, model Gemini 3.7 Flash by default
   unless the decomposition itself looks hard.
3. Record the Vassal's session ID on the Epic via
   `court set-field <epic_id> vassal_session_id <session_id>`.
4. Report the Epic ID and Vassal session back to M'lord.
