---
description: Return a Quest from REVIEW or GATE back to WORKING with actionable deficiencies
agent: steward
---
Quest: $ARGUMENTS

Return the Quest to `WORKING` using `court advance <id> WORKING --note "Returned: <reason>"`.
Log the actionable feedback in the Quest record so the Serf can address it.
