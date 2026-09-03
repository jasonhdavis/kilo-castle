---
description: Resume a HELD Quest once its blocker/Audience is resolved
agent: steward
---
Quest: $ARGUMENTS

Confirm the blocker is actually resolved (e.g. the Audience decision was
made and logged in `.court/LEDGER.md` and the Quest's "Audience Log") before
resuming. Then:
```
python3 .court/engine/cli.py advance <id> WORKING --note "Resumed: <what was resolved>"
```
(or back to whichever stage it was actually blocked at, if not WORKING).
