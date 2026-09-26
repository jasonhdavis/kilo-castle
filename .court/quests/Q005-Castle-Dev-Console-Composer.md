---
id: Q005-Castle-Dev-Console-Composer
title: Console composer: direct session chat without VS Code
kind: quest
app: castle
concern: dev-console-composer
parent_epic: Q003-Castle-Pb-App-Dev-Console
section: Feature
tags: Feature,UI
status: PLANNED
cogship_id: 
cogship_station: 
cogship_promoted_commit: 
task_file: 
pillory_of: 
pilloried_by: 
scout_of: 
branch: quest/q003/q005-castle-dev-console-composer
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

# Q005-Castle-Dev-Console-Composer — Console composer: direct session chat without VS Code

## Castle Ledger

- **2026-09-26T22:06:52Z** — - → OPEN: Quest created
- **2026-09-26T22:07:08Z** — OPEN → PLANNED: Chartered via composite `court charter`

## The Kingdom Requires

Add a composer panel to the court ui console (builds on Q004) that lets the operator send prompts directly to any session agent — Steward, Artist, or Serf — from the browser, with no VS Code window involved.

Transport: implement whichever mechanism Q001-Castle-Kilo-Serve-Api-Probe recommends (thin client over the local kilo serve HTTP API, or headless kilo run per turn), with the Q001 evidence pasted in this quest's charter notes at dispatch time. Requirements common to both:
- Per-session send box + streaming response display (SSE or chunked rendering).
- Session context is auto-injected: current court status summary (courts' quest tree delta since last visit) prepended to Steward-bound turns.
- Artist-direct mode: for artist sessions, the tab shows the dev server URL as an iframe/link alongside the composer so UI review happens in one place without a Steward relay.
- Prompts are logged to the session (land in kilo.db like any other turn) — the console must not create a parallel message store.
- No third-party dependencies beyond what Q004 already uses.

M'Lord's Charter Notes: GATED on Q001-Castle-Kilo-Serve-Api-Probe: do not dispatch until Q001 levies and its transport recommendation is folded into this quest's notes. M'Lord directive 2026-09-26: composer is the primary action of the console.

## Expected Tribute

- [ ] Composer sends a real prompt to a real session via the chosen transport (pasted request + response evidence)
- [ ] Streaming render verified end-to-end (pasted log/timing)
- [ ] Steward-bound turns include injected court status (pasted turn showing injection)
- [ ] Artist-direct tab shows dev server URL without Steward relay (screenshot-level description + curl)
- [ ] All turns land in kilo.db (query evidence)
- [ ] Clean tree, deferred rebase (behind: 0), self-advance to TRIBUTE_READY


- [ ] **Self-advance (do this LAST)**: once Tribute is rendered and `git rev-list --count HEAD..castle` is 0, run `python3 -m court.cli advance Q005-Castle-Dev-Console-Composer TRIBUTE_READY` yourself. Nothing else flips the status out of WORKING.

## Tribute Rendered

## Master of Coin's Audit

## Judgement of the Condemned

## Cogship Log

## Audience Log
