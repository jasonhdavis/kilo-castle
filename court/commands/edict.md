---
description: Record or display royal edicts and strategic priorities from M'Lord
agent: steward
---
Edict: $ARGUMENTS

1. If arguments are provided: record the decree into `.court/EDICTS.md` using `python3 -m court.cli edict "$ARGUMENTS"`.
2. If no arguments are provided: display all active decrees from `python3 -m court.cli edict`.
3. Confirm the recorded edict to M'Lord.
