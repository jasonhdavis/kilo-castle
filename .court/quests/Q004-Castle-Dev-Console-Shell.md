---
id: Q004-Castle-Dev-Console-Shell
title: court ui: zero-dependency localhost dev console shell
kind: quest
app: castle
concern: dev-console-shell
parent_epic: Q003-Castle-Pb-App-Dev-Console
section: Feature
tags: Feature,UI,Optimization
status: WORKING
cogship_id: 
cogship_station: 
cogship_promoted_commit: 
task_file: 
pillory_of: 
pilloried_by: 
scout_of: 
branch: quest/q003/q004-castle-dev-console-shell
worktree: /Users/scrummage/Python/kilo-castle/.kilo/worktrees/quest-q003-q004-castle-dev-console-shell
serf_session_id: ses_f203ea317ffetXQ21GQxi1Ooph
serf_model: GLM-5.3-Flash
master_of_coin_session_id: 
master_of_coin_model: 
gatekeeper_session_id: 
gatekeeper_model: 
artist_session_id: 
artist_model: 
vassal_session_id: 
created_at: 2026-09-26T22:06:22Z
updated_at: 2026-09-26T22:06:26Z
---

# Q004-Castle-Dev-Console-Shell — court ui: zero-dependency localhost dev console shell

## Castle Ledger

- **2026-09-26T22:06:22Z** — - → OPEN: Quest created
- **2026-09-26T22:06:22Z** — OPEN → PLANNED: Chartered via composite `court charter`
- **2026-09-26T22:06:26Z** — PLANNED → DISPATCHED: Serf dispatched (`court dispatch`)
- **2026-09-26T22:06:26Z** — DISPATCHED → WORKING: Serf toiling in worktree (`court dispatch`)

## The Kingdom Requires

Implement a `court ui` command in the court engine (this repo, python3 -m court.cli) that serves a single-page localhost web console with ZERO third-party dependencies (stdlib http.server + sqlite3 + subprocess + server-rendered HTML + small vanilla JS). It must open in the developer's existing browser; it must NOT ship or require Electron/Chromium/node build tooling.

Layout (single page, auto-refresh via 5s polling or SSE):
1. Left sidebar: quest pipeline sections (court tree: epics, quests grouped by status) with branch names. The castle integration trunk pinned at top. Each quest row shows: id, slug, status, branch, worktree path, dirty/behind state.
2. Main area, one tab-group per worktree: session tabs for every kilo session bound to that worktree directory (read-only sqlite at ~/.local/share/kilo/kilo.db: session table — id, title/slug, agent, model, time_updated, tokens_input/output). Tabs render a compact session card; clicking loads the session's recent messages (message/part tables, last ~50, read-only).
3. Process/memory panel (top bar or collapsible): all running kilo serve/run processes with PID, RSS, elapsed, and their child processes flagged by type (MCP servers such as shopify-dev-mcp, npm/npx wrappers, chromium, pytest). Orphaned children (parent is a kilo process but no active session) highlighted. A Reap action per child POSTs to the server which terminates exactly that whitelisted child PID (must verify its parent is a kilo process before killing; refuse anything else).
4. Data sources are strictly read-only EXCEPT the reap action: court state via the existing court.cli modules (import, do not shell out where an API exists), kilo.db opened with mode=ro.

Constraints:
- No third-party pip/npm dependencies, no frontend framework, no build step. One new module (e.g. court/ui_server.py) plus minimal cli registration.
- The server binds 127.0.0.1 only.
- Kill/reap is the ONLY mutating endpoint and must re-verify process parentage at request time.
- Handle kilo.db being 100+ GB: never full scans; query by indexed columns / recent rows only (ORDER BY time_updated DESC LIMIT n).
- Verify with: python3 -m court.cli ui --port 8765 & then curl the page, screenshot-level description in the report, and ps/curl output proving the process panel and reap whitelist behave (test reap against a spawned dummy child of a fake kilo-named parent, NOT a real session).

M'Lord's Charter Notes: M'Lord directive 2026-09-26: ASAP — cut VS Code from the pb-app dev cycle. This is the first shippable slice; composer (transport per Q001 probe) and MCP toggle panel follow.

## Expected Tribute

- [ ] court ui starts a 127.0.0.1-only server with zero third-party imports (pasted pip freeze check / import list)
- [ ] Sidebar renders court tree sections with branches, castle trunk on top (pasted HTML snippet or curl output)
- [ ] Session tabs per worktree render live session data from kilo.db in read-only mode (pasted curl output showing real sessions)
- [ ] Process panel lists real kilo processes with RSS + flagged children (pasted output matching ps)
- [ ] Reap endpoint verifies parentage and refuses non-whitelisted PIDs (pasted refusal + successful dummy-child reap evidence)
- [ ] kilo.db queries proven bounded (no full scans: EXPLAIN QUERY PLAN pasted)
- [ ] Clean tree, deferred rebase (behind: 0), self-advance to TRIBUTE_READY


- [ ] **Self-advance (do this LAST)**: once Tribute is rendered and `git rev-list --count HEAD..castle` is 0, run `python3 -m court.cli advance Q004-Castle-Dev-Console-Shell TRIBUTE_READY` yourself. Nothing else flips the status out of WORKING.

## Tribute Rendered

## Master of Coin's Audit

## Judgement of the Condemned

## Cogship Log

## Audience Log
