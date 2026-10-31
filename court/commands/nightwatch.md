---
description: Roll up where the Night's Watch is stuck and set up the manual work it cannot do
agent: steward
---
Arguments: $ARGUMENTS

Night's Watch triage:
1. `python3 -m court.cli nightwatch --report --repo <repo>` — a read-only digest from the journal + state: **livelock collects** that packed nothing, **standing escalations** (stale gatekeeper stamps, PENDING UI review), **goad/coin budget** usage (`serfs x goads_per_serf` before a Quest bubbles up), **failed actions**, **steward musters**, and **snapshot failures/restarts**.
2. `python3 -m court.cli nightwatch --status --repo <repo>` — daemon presence, last tick, per-Quest counters, and the 24-hour Steward muster tally.
3. Act on what the watch cannot: summon a Court Artist (`/artist`, `/atelier`) for PENDING UI review, regate or clear a stale gatekeeper stamp, tear down a stale gatehouse worktree, and triage anything that reached its worker-recovery budget. The watch never improvises work; it only rolls the mechanical verbs under hard caps.

Opt-in last resort — steward muster:
- `court nightwatch --enable goad,coin,collect,dispatch,steward` lets the watch summon ONE single-turn Steward triage session when the board has been unchanged for 45m, nothing is running anywhere, and a standing blocker remains. It is cooldown-capped (6h) and daily-capped (2), and it never re-prompts.
- Test the prompt without spawning: `python3 -m court.cli muster --why "manual check" --print-prompt`.
