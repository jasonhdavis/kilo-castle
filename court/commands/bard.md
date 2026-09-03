---
description: Review ballads across an Epic, App, or recent Quests into a narrative summary
agent: steward
---
Arguments: $ARGUMENTS

Follow the Bard Protocol:
1. Run `python3 .court/engine/cli.py rollup --section ballad $ARGUMENTS` to deterministically extract all Ballad sections across target Quests.
2. Synthesize the extracted ballads into a clean, executive narrative arc:
   - What challenges were encountered.
   - What architectural decisions were made.
   - What has been shipped and transformed.
3. Present the chronicled story concisely to M'Lord.
