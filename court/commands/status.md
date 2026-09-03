---
description: Full Court dashboard — active Quests, Serfs, and pending Audiences
agent: steward
---
Reconstruct current state exactly per the Steward's durable-memory protocol:

1. `python3 .court/engine/cli.py status`
2. `agent_manager` `list`
3. Cross-reference the two, then render the Observer dashboard shape from
   `.kilo/prompts/steward.md` §"Status / Observer output shape":
   - 🔴 Pending Audiences (always first)
   - 🟡 REVIEW/GATE
   - 🔵 WORKING (active Serfs, with live Agent Manager activity state)
   - ⚪ READY_FOR_TEARDOWN
Keep it short. Only go deeper into a single Quest if M'lord asks.
