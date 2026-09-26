---
id: Q006-Castle-Dev-Console-Mcp-Control
title: MCP control: enable/disable servers per need from the console
kind: quest
app: castle
concern: dev-console-mcp-control
parent_epic: Q003-Castle-Pb-App-Dev-Console
section: Feature
tags: Feature,Optimization,UI
status: PLANNED
cogship_id: 
cogship_station: 
cogship_promoted_commit: 
task_file: 
pillory_of: 
pilloried_by: 
scout_of: 
branch: quest/q003/q006-castle-dev-console-mcp-control
worktree: 
serf_session_id: 
serf_model: 
master_of_coin_session_id: 
master_of_coin_model: 
gatekeeper_session_id: 
gatekeeper_model: 
artist_session_id: 
artist_model: 
vassal_session_id: 
created_at: 2026-09-26T22:06:52Z
updated_at: 2026-09-26T22:07:08Z
---

# Q006-Castle-Dev-Console-Mcp-Control — MCP control: enable/disable servers per need from the console

## Castle Ledger

- **2026-09-26T22:06:52Z** — - → OPEN: Quest created
- **2026-09-26T22:07:08Z** — OPEN → PLANNED: Chartered via composite `court charter`

## The Kingdom Requires

Add MCP control to the court ui console (builds on Q004): make MCP spawn behavior visible and controllable per need.

1. MCP inventory view: merged MCP config for the selected project (global ~/.config/kilo/kilo.jsonc + project kilo.json + any legacy roots), each entry showing name, type (local/remote), enabled flag, and — if a process is currently running for it — PID + RSS.
2. Toggle action: flip the enabled flag in the project's kilo.json (surgical JSON edit preserving formatting/comments where feasible; fall back to a documented rewrite if not). Toggling writes the config and shows a notice that running sessions keep old MCPs until restarted — the console does NOT hot-kill live MCPs from a toggle.
3. Session standup check: when the console renders a worktree whose project has a local MCP enabled AND ≥2 concurrent sessions, show a duplicate-spawn warning with the projected memory cost (per pair, measured: ~230 MB).
4. Profiled targets for pb-app: shopify-dev MCP provides documentation — default OFF for headless serfs, ON for interactive Shopify sessions; document this recommendation in the toggle UI tooltip.
5. No third-party dependencies beyond Q004's set.

M'Lord's Charter Notes: M'Lord directive 2026-09-26: MCP enable/disable per need is approved. Shopify dev MCP (documentation) ON for interactive sessions, OFF for headless serfs. Dispatch after Q004 shell lands to avoid cli registration conflicts in the same module.

## Expected Tribute

- [ ] MCP inventory renders the real merged config for pb-app (pasted output matching the three config sources)
- [ ] Toggle flips enabled in pb-app kilo.json surgically (before/after diff pasted; invalidates nothing else)
- [ ] Duplicate-spawn warning fires on 2+ concurrent sessions with measured memory projection (pasted)
- [ ] Toggle does not kill live processes; restart notice shown (evidence)
- [ ] Clean tree, deferred rebase (behind: 0), self-advance to TRIBUTE_READY


- [ ] **Self-advance (do this LAST)**: once Tribute is rendered and `git rev-list --count HEAD..castle` is 0, run `python3 -m court.cli advance Q006-Castle-Dev-Console-Mcp-Control TRIBUTE_READY` yourself. Nothing else flips the status out of WORKING.

## Tribute Rendered

## Master of Coin's Audit

## Judgement of the Condemned

## Cogship Log

## Audience Log
