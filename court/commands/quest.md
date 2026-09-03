---
description: Create a new Quest (bug/feature/task) and dispatch it
agent: steward
---
M'lord wants: $ARGUMENTS

Follow the Steward protocol's Intake step exactly (see `.kilo/prompts/steward.md`
§"1. Intake"):
1. Determine `app` and `concern`. Grep `tasks/ACTIVE.md`, the relevant
   `tasks/apps/<app>/` folder, and the codebase for duplication risk first.
2. Determine the Agent Manager section (`Bug fix`, `Feature`,
   `Optimization`, or `Investigation`) per `AGENTS.md`'s existing rules.
3. Create the Quest:
   ```
   python3 .court/engine/cli.py new --app <app> --concern <concern> \
     --title "<short title>" --section "<section>" --goal "<Goal & Scope>"
   ```
4. Write the full Expected Tribute checklist via `set-section`.
5. Immediately commit the new Quest file to `castle` (e.g. `git add .court/quests/<id>.md && git commit -m "court: create <id> (<title>)"`) to keep `castle` clean.
6. If this is genuinely a larger multi-Quest initiative instead, use
   `/epic` rather than `/quest` — say so and stop here if that's the case.
7. Report the new Quest ID back to M'lord, and ask whether to dispatch a
   Serf immediately or hold for further scoping.
