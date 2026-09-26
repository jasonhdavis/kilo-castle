---
id: Q002-Pb-App-Mcp-Dedupe-Memory-Hygiene
title: MCP config dedupe and kilo session memory hygiene
kind: quest
app: pb-app
concern: mcp-dedupe-memory-hygiene
parent_epic: 
section: Optimization
tags: Optimization,Hygiene
status: WORKING
cogship_id: 
cogship_station: 
cogship_promoted_commit: 
task_file: 
pillory_of: 
pilloried_by: 
scout_of: 
branch: quest/q002-pb-app-mcp-dedupe-memory-hygiene
worktree: /Users/scrummage/Python/kilo-castle/.kilo/worktrees/quest-q002-pb-app-mcp-dedupe-memory-hygiene
serf_session_id: ses_f204c7ad3ffeEkCFL11HqwLFrZ
serf_model: GLM-5.3-Flash
master_of_coin_session_id: 
master_of_coin_model: 
gatekeeper_session_id: 
gatekeeper_model: 
artist_session_id: 
artist_model: 
vassal_session_id: 
created_at: 2026-09-26T21:50:12Z
updated_at: 2026-09-26T21:51:19Z
---

# Q002-Pb-App-Mcp-Dedupe-Memory-Hygiene — MCP config dedupe and kilo session memory hygiene

## Castle Ledger

- **2026-09-26T21:50:12Z** — - → OPEN: Quest created
- **2026-09-26T21:51:15Z** — OPEN → PLANNED: Chartered via composite `court charter`
- **2026-09-26T21:51:19Z** — PLANNED → DISPATCHED: Serf dispatched (`court dispatch`)
- **2026-09-26T21:51:19Z** — DISPATCHED → WORKING: Serf toiling in worktree (`court dispatch`)

## The Kingdom Requires

Reduce resident memory and config duplication in the Kilo toolchain.

Observed on 2026-09-26 (live profile): shopify-dev-mcp is spawned TWICE (~390 MB RSS) because it is defined in three overlapping config sources in pb-app: kilo.json (mcp.shopify-dev), .kilocode/mcp.json, and .roo/mcp.json. Orphaned child processes (npm wrappers, leftover pytest runs) are retained under kilo serve after runs complete. The Kilo session database (~/.local/share/kilo/kilo.db) has grown to 104 GB across 5,436 sessions.

Required work:
1. Verify kilo.json mcp.shopify-dev is the sole authoritative definition, then delete the legacy .kilocode/mcp.json and .roo/mcp.json files (they are obsolete config roots; the environment standard is .kilo/). Preserve the brevo MCP definition by moving it into kilo.json mcp section first if it is still wanted — confirm the brevo server entry before dropping it.
2. Confirm post-fix behavior: restart scenario documented showing only ONE shopify-dev-mcp child per serve process (paste ps output).
3. Produce a kilo.db compaction runbook: verified backup command (with pasted output and resulting file size), a dry-run estimate of reclaimable space (VACUUM on a COPY of the db, sizes recorded before/after), and the exact safe sequence incl. stopping serve processes first. DO NOT run destructive compaction against the live db in this quest — runbook + backup only.
4. Write a short section documenting how orphaned child processes can be reaped on session teardown (observation only; implementation in court CLI is out of scope for this quest).

M'Lord's Charter Notes: M'Lord directive 2026-09-26: immediate memory triage approved. MCP dedupe is the fast RAM win (~390 MB of duplicate shopify-dev-mcp). DB compaction is runbook+backup only this quest; the actual destructive compaction is a separate gated action after M'Lord reviews the numbers.

## Expected Tribute

- [ ] Legacy .kilocode/mcp.json and .roo/mcp.json removed; brevo entry preserved (or explicitly confirmed unwanted) in kilo.json
- [ ] Post-fix ps output pasted showing exactly one shopify-dev-mcp child per serve process
- [ ] kilo.db backup created with pasted command output and backup size
- [ ] VACUUM dry-run on a db COPY with before/after sizes pasted (match: estimated reclaim %)
- [ ] Compaction runbook (safe sequence incl. stopping serve) written into the quest's Tally
- [ ] Deferred rebase: git merge castle complete, behind: 0, clean tree, self-advance to TRIBUTE_READY


- [ ] **Self-advance (do this LAST)**: once Tribute is rendered and `git rev-list --count HEAD..castle` is 0, run `python3 -m court.cli advance Q002-Pb-App-Mcp-Dedupe-Memory-Hygiene TRIBUTE_READY` yourself. Nothing else flips the status out of WORKING.

## Tribute Rendered

## Master of Coin's Audit

## Judgement of the Condemned

## Cogship Log

## Audience Log
