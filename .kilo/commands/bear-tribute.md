---
description: Demand that a Serf submit its formal 5-part Tribute report (Ballad, Tribute, Penance, Audience, Humble Opinion)
agent: steward
---
Target: $ARGUMENTS

Follow the Bear Tribute Protocol:
1. Identify the target Quest and its Serf session ID.
2. Prompt the Serf using `.court/templates/bear_tribute_prompt.md`.
3. Ensure the Serf writes its completion into `.court/quests/<id>.md` under `# Tribute Rendered`.
4. Ingest the rendered report and advance the Quest to `REVIEW`.
