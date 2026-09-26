---
id: Q001-Castle-Kilo-Serve-Api-Probe
title: Kilo serve HTTP API probe for Castle UI architecture
kind: scout
app: castle
concern: kilo-serve-api-probe
parent_epic: 
section: Investigation
tags: Investigation,Optimization
status: WORKING
cogship_id: 
cogship_station: 
cogship_promoted_commit: 
task_file: 
pillory_of: 
pilloried_by: 
scout_of: 
branch: scout/q001-castle-kilo-serve-api-probe
worktree: /Users/scrummage/Python/kilo-castle/.kilo/worktrees/scout-q001-castle-kilo-serve-api-probe
serf_session_id: ses_f204d2ee8ffe49UMD3G6dEn8wk
serf_model: GLM-5.3-Flash
master_of_coin_session_id: 
master_of_coin_model: 
gatekeeper_session_id: 
gatekeeper_model: 
artist_session_id: 
artist_model: 
vassal_session_id: 
created_at: 2026-09-26T21:49:52Z
updated_at: 2026-09-26T21:50:33Z
---

# Q001-Castle-Kilo-Serve-Api-Probe — Kilo serve HTTP API probe for Castle UI architecture

## Castle Ledger

- **2026-09-26T21:49:52Z** — - → OPEN: Quest created
- **2026-09-26T21:50:29Z** — OPEN → PLANNED: Chartered via composite `court charter`
- **2026-09-26T21:50:33Z** — PLANNED → DISPATCHED: Scout dispatched (`court dispatch`)
- **2026-09-26T21:50:33Z** — DISPATCHED → WORKING: Scout toiling in worktree (`court dispatch`)

## The Kingdom Requires

Determine whether a lightweight Castle UI (no VS Code, no Electron) can be built as a thin client over the local `kilo serve` HTTP API, versus driving headless `kilo run` subprocesses per turn.

Background: two `kilo serve` backends run at ~1.1-1.5 GB RSS each; the VS Code Electron tree adds ~3.1 GB. The serve processes listen on localhost ports (observed: 127.0.0.1:4096 and 127.0.0.1:49204) and respond 401 unauthenticated, so a local auth token mechanism exists.

Required investigation (research + probing only; NO production code, NO config changes):
1. Locate the local auth token the VS Code extension uses to authenticate to kilo serve (check extension storage, env vars, local config files, kilo CLI flags such as --help on the serve/run subcommands).
2. Enumerate the serve HTTP API: session list, session create, send prompt/message, stream response events (SSE/WebSocket), permission/request handling. Record method, path, auth header format, and one curl example per endpoint.
3. Determine how a headless CLI turn works: `kilo run --agent steward` invocation shape, how to pass a prompt non-interactively, how output/session id are returned, and whether session state lands in ~/.local/share/kilo/kilo.db in a form a UI could read.
4. Compare the two architectures on: memory footprint, latency, streaming fidelity, ability to resume the EXISTING Steward session, and process lifecycle (who spawns/kills serve).
5. Deliver a written recommendation with evidence (pasted curl output).

M'Lord's Charter Notes: M'Lord directive 2026-09-26: dispatch spike immediately. This scout gates the Castle UI epic. Deliverable is a doc + recommendation; the production UI quest will implement the chosen architecture. UI direction recorded separately in ledger.

## Expected Tribute

- [ ] Auth mechanism located and documented (exact token source and header format)
- [ ] API endpoint inventory with working curl examples (session list, send prompt, stream) pasted as real output
- [ ] Headless `kilo run` turn documented with real command + output
- [ ] Architecture comparison table (thin-client vs headless) with a single clear recommendation
- [ ] Findings written to .court/quests/<id>.md under ## Tribute Rendered; zero changes to production code or config


- [ ] **Self-advance (do this LAST)**: once Tribute is rendered and `git rev-list --count HEAD..castle` is 0, run `python3 -m court.cli advance Q001-Castle-Kilo-Serve-Api-Probe TRIBUTE_READY` yourself. Nothing else flips the status out of WORKING.

## Tribute Rendered

## Master of Coin's Audit

## Judgement of the Condemned

## Cogship Log

## Audience Log
