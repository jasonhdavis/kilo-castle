---
id: Q003-Castle-Pb-App-Dev-Console
title: pb-app Development Console: cut VS Code from the dev pipeline
kind: epic
app: castle
concern: pb-app-dev-console
parent_epic: 
section: 
tags: 
status: OPEN
cogship_id: 
cogship_station: 
cogship_promoted_commit: 
task_file: 
pillory_of: 
pilloried_by: 
scout_of: 
branch: epic/q003-castle-pb-app-dev-console
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
created_at: 2026-09-26T22:05:57Z
updated_at: 2026-09-26T22:05:57Z
---

# Q003-Castle-Pb-App-Dev-Console — pb-app Development Console: cut VS Code from the dev pipeline

## Castle Ledger

- **2026-09-26T22:05:57Z** — - → OPEN: Quest created

## The Kingdom Requires

Build a lightweight local web console that replaces VS Code + the Kilo extension in the pb-app development pipeline. Motivation (profiled 2026-09-26): VS Code Electron tree 3.08 GB + 1.1-1.5 GB per kilo serve backend + duplicate MCP spawns per session; Agent Manager is unreliable against the court pipeline; artist UI sessions are painful when routed through a Steward intermediary.

Target experience:
- Left sidebar: pipeline sections with branches; the castle/integration trunk at top; each quest worktree listed under its section.
- Session tabs inside each worktree row, mirroring Agent Manager.
- Primary action: a composer that talks directly to session agents (Steward, Artist, Serfs) without a VS Code window.
- Live process/memory panel: kilo serve/run processes, their MCP/chromium children, RSS per process, with reaping actions. Non-duplicate MCPs on session standup.
- MCP control: enable/disable servers per need (e.g. Shopify dev docs MCP for interactive sessions, off for headless serfs).
- Artist-direct mode: open the running dev server URL and chat with the Artist session directly, without a Steward relay.

Quests: UI-1 shell (dispatched first), UI-2 composer (gated on Q001 serve-API probe), UI-3 MCP control panel.

## Expected Tribute

- [ ] **Self-advance (do this LAST)**: once Tribute is rendered and `git rev-list --count HEAD..castle` is 0, run `python3 -m court.cli advance Q003-Castle-Pb-App-Dev-Console TRIBUTE_READY` yourself. Nothing else flips the status out of WORKING.

## Tribute Rendered

## Master of Coin's Audit

## Judgement of the Condemned

## Cogship Log

## Audience Log
