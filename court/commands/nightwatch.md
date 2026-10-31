---
description: Roll up where the Night's Watch is stuck and set up the manual work it cannot do
agent: steward
---
Arguments: $ARGUMENTS

Night's Watch triage:
1. `python3 -m court.cli nightwatch --report --repo <repo>` — a read-only digest from the journal + state: **livelock collects** that packed nothing, **standing escalations** (stale gatekeeper stamps, PENDING UI review), **goad/coin budget** usage (`serfs x goads_per_serf` before a Quest bubbles up), **failed actions**, and **snapshot failures/restarts**.
2. `python3 -m court.cli nightwatch --status --repo <repo>` — daemon presence, last tick, and per-Quest counters.
3. Act on what the watch cannot: summon a Court Artist (`/artist`, `/atelier`) for PENDING UI review, regate or clear a stale gatekeeper stamp, tear down a stale gatehouse worktree, and triage anything that reached its worker-recovery budget. The watch never improvises work; it only rolls the mechanical verbs under hard caps.
