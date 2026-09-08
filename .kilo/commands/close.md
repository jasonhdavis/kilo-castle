---
description: Close a DONE Quest and archive its record
agent: steward
---
Quest: $ARGUMENTS

Only close a Quest that has actually reached `DONE` (merged, and any
teardown M'lord wanted is complete) — not just `READY_TO_RAZE`.
```
python3 -m court.cli advance <id> DONE --note "Closed"
python3 -m court.cli archive <id>
```
Archiving moves the file to `.court/archive/` — it stays readable, it is
never deleted.
