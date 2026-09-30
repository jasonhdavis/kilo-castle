---
id: Q001-Castle-Dev-Console-Monitor-View
title: Castle Dev Console: Integrate Monitor as In-App View with Theme Support
kind: quest
app: castle
concern: dev-console-monitor-view
parent_epic: 
section: Feature
tags: Feature
status: PLANNED
cogship_id: 
cogship_station: 
cogship_promoted_commit: 
task_file: 
pillory_of: 
pilloried_by: 
scout_of: 
branch: quest/q001-castle-dev-console-monitor-view
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
created_at: 2026-09-30T00:13:21Z
updated_at: 2026-09-30T00:13:59Z
---

# Q001-Castle-Dev-Console-Monitor-View — Castle Dev Console: Integrate Monitor as In-App View with Theme Support

## Castle Ledger

- **2026-09-30T00:13:21Z** — - → OPEN: Quest created
- **2026-09-30T00:13:59Z** — OPEN → PLANNED: Chartered via composite `court charter`

## The Kingdom Requires

M'Lord's Charter Notes: 
Goal & Scope (plain engineering requirements, out-of-character):

The Castle dev console (court/ui_server.py, main page served at /) currently links its 'monitor' tab to a standalone page served at /monitor (MONITOR_PAGE constant, ~line 4573). That standalone page (a) is visually disconnected from the console shell, and (b) hardcodes a dark-only GitHub-dark palette in its own :root block, so it ignores the console's existing light/dark theme system (html[data-theme], toggleTheme(), localStorage 'castle-theme').

This Quest folds the monitor into the main console UI as a native in-app view, exactly like the existing 'board' and 'settings' views:

1. Replace the <a class="app" id="v_monitor" href="/monitor"> external link in the console header with a real tab (div.app id v_monitor) that calls setView('monitor'), and add a <section id="monitor"> to the console page hosting the monitor content (summary chips, Agent Sessions table, Schedules table with delete action, Processes table with kill action, refresh button, updated-stamp, flags legend).
2. Port the monitor view's markup and behavior from MONITOR_PAGE into the console shell. Reuse the console's shared CSS variables and existing component classes where they fit; any monitor-specific CSS must use var(...) tokens that resolve correctly under BOTH html[data-theme=light] and html[data-theme=dark]. Add light-theme overrides to the existing html[data-theme=light] block if needed. The global theme toggle must apply live while the monitor view is visible.
3. Preserve all functionality: /api/monitor fetch + 10s auto-refresh + manual refresh, kill-process confirm flow, delete-schedule confirm flow, summary chips, badges, relative-time formatting. Keep the pure helper functions in ui_server.py (select_monitor_processes, build_monitor_snapshot, _monitor_state, etc.) and all API routes (/api/monitor, /api/monitor/kill, /api/monitor/schedule/delete) unchanged.
4. The standalone /monitor route must stop serving a separate page: redirect it to / (302) so existing bookmarks keep working. Remove MONITOR_PAGE if it becomes dead code; do not leave duplicated monitor CSS/markup in two places.
5. Keep the monitor view's table sections scrollable/laid out sensibly inside the console's <main> layout (the console view switching already hides/shows sections — follow the pattern used by #board and #settings).

Constraints:
- No changes to backend data collection, process-selection logic, or API payload shapes.
- No internal Court/Castle roleplay vocabulary in any UI copy (strings, titles, tooltips, badges) — plain professional product language only.
- tests/test_monitor.py must be updated for the removed standalone page (e.g. /monitor now redirects; API endpoints asserted as before) and must pass.

Out of scope: new monitor features (filtering, sorting, historical charts), changes to schedule/wakeup stores, mobile-specific layouts.

## Expected Tribute

- [ ] **Self-advance (do this LAST)**: once Tribute is rendered and `git rev-list --count HEAD..castle` is 0, run `python3 -m court.cli advance Q001-Castle-Dev-Console-Monitor-View TRIBUTE_READY` yourself. Nothing else flips the status out of WORKING.

## Tribute Rendered

## Master of Coin's Audit

## Judgement of the Condemned

## Cogship Log

## Audience Log
