# The Court Ledger

Durable running log of Steward decisions, cross-Quest coordination, and Audience records.

## Standing Architectural Decisions

| Date | Topic | Decision | Notes |
|---|---|---|---|
| 2026-09-03 | Initialization | Court initialized with Kilo Castle | Standalone multi-agent orchestration layer active |

## Audience Log Index

| Date | Quest | Decision Required | Verdict / Outcome |
|---|---|---|---|

## 2026-09-26 — Kilo Memory Profile & Castle UI Plan (reconstructed; prior session lost)

Lost session on "custom castle UI to cut kilo memory" could not be recovered from
local session history (searched all 22 sessions). Reconstructed from live profiling.

### Live memory profile (2026-09-26, 13.1 GB system RSS)
- VS Code Electron tree: **3.08 GB** (20 procs) — Kilo webview rides this.
- `kilo serve` backends: **2.47 GB** across TWO instances (one per VS Code window):
  pid 83314 (pb-app, 1.45 GB), pid 93580 (kilo-castle, 1.11 GB).
- Duplicate shopify-dev-mcp: **390 MB** (4 procs) — defined in THREE config sources
  (pb-app `kilo.json` mcp.shopify-dev, `.kilocode/mcp.json`, `.roo/mcp.json`).
- Orphaned pytest + npm children retained under serve after run completion.
- `~/.local/share/kilo/kilo.db`: **104 GB**, 5,436 sessions / 318k messages / 1.26M parts.
- Kilo-related stack total: ~5.5 GB. Chrome (unrelated, for contrast): 2.65 GB.

### Plan: Court UI on top of court CLI + kilo CLI (no VS Code, no Electron)
Quests (proposed epic, pending Council/assent):
- Q-SPIKE: Probe the local `kilo serve` HTTP API (ports 4096/49204; auth-gated 401).
  Deliverable: doc of endpoints (session list, send prompt, stream). Decision:
  thin-client on serve API vs headless `kilo run` per turn.
- Q-UI-1: `court ui` — stdlib/zero-dep local web server rendering status/tree/rollup
  as auto-refreshing HTML. Court status implicit in UI.
- Q-UI-2: Action layer — buttons wrapping CLI ops (dispatch, goad, coin, advance,
  charter) with confirmation; subprocess-only, no daemon logic beyond serve.
- Q-UI-3: Steward composer — chat panel bound to the Steward session as primary
  action; court status auto-injected into turns.
- Q-HYGIENE: dedupe MCP configs (delete legacy `.kilocode/` + `.roo/`), compact/archive
  kilo.db, reap orphaned child processes on session teardown.
Memory math: replace per-window VS Code (3.1 GB) + serve (1.2–1.5 GB) with stdlib
server (~30 MB) + existing browser tab (~80 MB) + at most one shared serve.
Estimated savings: 4–6 GB; disk: up to ~100 GB via db compaction.
