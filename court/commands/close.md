---
description: Close a DONE Quest and archive its record
agent: steward
---
Quest: $ARGUMENTS

Only close a Quest that has actually reached `DONE` (merged, and any
teardown M'lord wanted is complete) — not just `READY_FOR_TEARDOWN`.
```
python3 .court/engine/cli.py advance <id> DONE --note "Closed"
python3 .court/engine/cli.py archive <id>
```
Archiving moves the file to `.court/archive/` — it stays readable, it is
never deleted.
