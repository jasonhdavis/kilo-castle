---
description: Mark a Quest HELD pending an Audience or external blocker
agent: steward
---
Quest: $ARGUMENTS

```
python3 -m court.cli advance <id> HELD --note "<reason>"
```
`HELD` is a side-state, not a pipeline failure — use it when a Quest is
blocked on M'lord's decision (an Audience) or an external dependency, not
as a substitute for `/return` (which is for sending flawed work back to
WORKING). If this is genuinely an Audience, raise it properly (see
`/audience` / the Steward's Audience protocol) rather than just parking
the Quest silently.
