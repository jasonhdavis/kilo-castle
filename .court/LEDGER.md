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

## 2026-09-26 — M'Lord UI directive + dispatches (Q001, Q002)

### UI layout direction (M'Lord, verbatim intent)
- Left sidebar: branches displayed inside their sections. Castle sits at the top;
  the Steward works on Castle (or a dedicated stewardship branch is developed).
- Inside each worktree row: session tabs, mirroring Kilo Code Agent Manager.
- Primary action remains the Steward composer.

### Dispatched
- Q001-Castle-Kilo-Serve-Api-Probe (scout, GLM-5.3-Flash) — WORKING in
  .kilo/worktrees/scout-q001-castle-kilo-serve-api-probe, branch
  scout/q001-castle-kilo-serve-api-probe, session ses_f204d2ee8ffe49UMD3G6dEn8wk.
  Gates the Castle UI epic; production UI quests will implement its recommendation.
- Q002-Pb-App-Mcp-Dedupe-Memory-Hygiene (serf, GLM-5.3-Flash) — WORKING in
  .kilo/worktrees/quest-q002-pb-app-mcp-dedupe-memory-hygiene, branch
  quest/q002-pb-app-mcp-dedupe-memory-hygiene, session ses_f204c7ad3ffeEkCFL11HqwLFrZ.
  Immediate memory triage: MCP dedupe (kill duplicate shopify-dev-mcp, ~390 MB),
  kilo.db backup via APFS clone + VACUUM dry-run on the clone (runbook only, no
  destructive compaction without M'Lord review).

### Next pipeline triggers
- Q001 done → levy → recommendation feeds Castle UI epic chartering (Q-UI-1..4).
- Q002 done → levy → MoC audit → GATE.

## 2026-09-26 — MCP-per-session bloat: root cause diagnosed

M'Lord correction: Castle UI epic serves the **pb-app development pipeline**, not
castle-on-castle. UI quests will be pb-app quests; kilo-castle remains tooling.

### Root cause (verified)
Kilo config schema (app.kilo.ai/config.json): AgentConfig has NO `mcp` field —
MCP servers CANNOT be scoped per agent. They boot per kilo PROCESS, at startup,
for every enabled MCP in the merged config. Every headless `kilo run` (serf,
scout, MoC, Gatekeeper) is its own kilo process.

pb-app merges MCP definitions from THREE sources:
- kilo.json: `shopify-dev` (local, npx @shopify/dev-mcp@latest) enabled
- .kilocode/mcp.json: `shopify-dev-mcp` (DUPLICATE, different name → merged as a
  second distinct server) + brevo (remote)
- .roo/mcp.json: further legacy overlap
- global ~/.config/kilo/kilo.jsonc: sentry-dev + sentry-picobarn-pb-app (remote)

Net: EVERY pb-app session (interactive or headless serf) spawns TWO
shopify-dev-mcp node process pairs (~230 MB each pair; ~480 MB observed under one
serve window with 2 sessions). Serfs running in pb-app worktrees inherit this
(worktrees contain pb-app's kilo.json; config resolution walks up from cwd).

Live confirmation: kilo-castle defines ZERO MCPs — the three headless sessions
dispatched today under kilo-castle have no MCP children. pb-app's serve process
earlier held 2 duplicate shopify pairs, dying with the window's sessions.

### Fix (the "easy solve")
1. Delete legacy .kilocode/mcp.json + .roo/mcp.json (Q002, dispatched, in flight).
2. One-line change: set `enabled: false` on shopify-dev in pb-app kilo.json —
   schema supports `{"enabled": false}` shorthand. Headless serfs don't need
   Shopify dev docs; enable it manually only in interactive Shopify sessions.
   Savings: ~240 MB per session × N parallel serfs, plus duplicate tool schemas
   removed from every prompt context.
3. Remote Sentry MCPs are connection-only (no process) but still inject tool
   schemas; consider disabling sentry-dev on serf-heavy machines if not used.

## 2026-09-26 — Epic Q003 chartered: pb-app Dev Console (VS Code cut from pipeline)

M'Lord ASAP directive: build the console NOW; Agent Manager unreliable, artist
sessions painful via Steward relay, memory a primary concern.

- Q004-Castle-Dev-Console-Shell DISPATCHED (serf ses_f203ea317ffftXQ21GQxi1Ooph):
  zero-dep `court ui` localhost console — sidebar sections/branches (castle trunk
  top), session tabs per worktree (kilo.db ro), live process/RSS panel with
  parentage-verified reap of orphaned MCP/chromium children.
- Q005-Castle-Dev-Console-Composer PLANNED, GATED on Q001 transport probe.
- Q006-Castle-Dev-Console-Mcp-Control PLANNED (after Q004): merged MCP inventory,
  surgical kilo.json toggle, duplicate-spawn warning. Policy: shopify dev docs MCP
  ON interactive / OFF headless serfs.
- Pipeline now: Q001 scout WORKING, Q002 hygiene serf WORKING, Q004 shell serf WORKING.

## 2026-09-26 — Pivot to direct development (M'Lord: NO QUEST INFRASTRUCTURE)

- Quest files Q001-Q006 removed from .court/ (numbering would collide with pb-app's
  Q694+ sequence if migrated); quest worktrees + branches torn down; workers stopped.
- RECOVERED LOST SESSION: pb-app .court/quests/Q694-Platform-Castle-Manager-Web-UI.md
  ("Lightweight Castle Manager Web UI", chartered 21:11Z tonight) is the lost work.
  Its serf had committed only paperwork; its charter design survived and is folded
  into the direct build. Left in pb-app for M'Lord to raze or reuse.
- DIRECT BUILD SHIPPED: `court ui` — court/ui_server.py + cli subcommand (commit
  5a28ea4). Zero-dep stdlib server on 127.0.0.1:8300: quest sidebar (castle trunk
  pinned), worktree cards with session tabs (kilo.db ro, bounded queries), message
  drawer (/api/session), live process panel with kilo-child flags and
  parentage-verified /api/reap. Verified live against real data.
- IN FLIGHT (background subagents, no court ceremony):
  - dev-console/mcp-panel worktree: MCP inventory + surgical toggle + backup +
    duplicate-spawn warning (subagent ses_f2030e186ffewIZImZ2VUujAA1).
  - kilo serve API probe (read-only research): auth token discovery, endpoint
    inventory, headless run shape, transport recommendation for the composer
    (subagent ses_f20311e99ffeA6fI8RPXwaJ027).

## 2026-09-26 (late) — MCP panel merged; composer shipped; transport decision made

- Merged dev-console/mcp-panel (4a204e9 → merge 9bfbdca): MCP inventory card
  (13 entries across global/pb-app/pb-app-legacy), toggle w/ .bak backup,
  duplicate-spawn warning; subagent also fixed the STATUS_ORDER template leak
  that had broken page rendering; JSONC toggles guarded (comment loss).
- Material Tailwind dark theme shipped (a5531ef): MT palette/pill chips/elevated
  cards, per M'Lord standard (docs in pb-custom).
- COMPOSER (primary action) shipped on HEADLESS TRANSPORT — decision made WITHOUT
  waiting for serve probe: `kilo run` natively supports --session <id> (continue
  existing sessions), --format json (streaming events), --dir, --agent. Composer:
  POST /api/send (whitelisted agents steward/code/serf/scout/artist; dirs limited
  to known worktrees+repo), event stream polled into drawer. Verified live.
  Serve-API probe still useful for attach-mode (kilo run --attach) later.
- Console live at http://127.0.0.1:8300: sidebar, session tabs, message drawer,
  process panel w/ reap, MCP panel, composer.

## 2026-09-27 — Serve API auth solved (direct probe, console-independent)

- Mechanism: HTTP **Basic auth** — user `kilo`, password = `KILO_SERVER_PASSWORD`
  env var held by the `kilo serve` process itself (verified: 401 unauthenticated →
  200 with `-u kilo:$KILO_SERVER_PASSWORD` on /config; config JSON returned).
- Implications for the console: attach-mode is viable — console can start its OWN
  standalone `kilo serve` (independent of VS Code) and run turns via
  `kilo run --attach http://127.0.0.1:<port> -u kilo -p <pw>`, giving one shared
  server + session continuity, instead of a spawn per turn. Current headless
  per-turn transport stays default (works, validated); attach-mode is the
  follow-up optimization when per-turn spawn overhead matters.
- Serve-API probe subagent resumed (final write-up of preserved context) to
  supplement endpoint inventory; no longer blocking anything.

## 2026-09-27 — Serve probe complete: endpoint inventory + architecture tradeoff

Probe findings (static extraction from kilo binary + extension.js; auth verified
live by Steward separately):
- Auth confirmed: Basic `kilo:$KILO_SERVER_PASSWORD`; also accepts
  `?auth_token=<b64>`; serve WITHOUT that env var runs unsecured. Extension
  generates 32-byte hex password at spawn; never persisted to disk (auth.json =
  provider creds only). serve has a parent-watchdog (KILO_PARENT_PID) — dies with
  its VS Code window. Legacy daemon file (~/.local/state/kilo/daemon.json) absent.
- 200+ HTTP ops behind auth, directory-scoped via x-kilo-directory header. Key:
  GET /session, /session/:id/message, /config, /project, /mcp/status,
  kilocode/agentManager; GET /event = SSE (session.created, message.part.updated,
  step-start/finish, session.error, permission.asked); POST /session (create),
  POST /session/:id/prompt_async (204 + SSE delivery), /session/:id/message
  (sync streaming), /abort, /fork.
- kilo run: prompt positional + appended from stdin when non-TTY; NDJSON events
  via --format json; --attach <url> -u/-p to a running serve; --session/-c reuse.
- ARCHITECTURE TRADEOFF (Steward analysis on top of probe recommendation):
  probe recommends console-owned serve + SSE thin client (warm model cache,
  native SSE fidelity, one server for all sessions). BUT a resident serve costs
  ~1.1-1.5 GB while M'Lord's primary concern is memory; headless per-turn
  (current composer) boots an embedded server per turn (~2-4 s + cache refetch)
  but keeps IDLE memory at ~zero when no turn is running. DECISION: keep
  headless per-turn as default (memory-first); add optional `--serve` mode
  (console-owned serve, prompt_async+SSE) as follow-up for heavy interactive
  use. Revisit when M'Lord weighs latency vs resident memory.

## 2026-09-27 (later) — Composer frozen-frame bug fixed; URL state tokens; auto session continuity

- FROZEN FRAME ROOT CAUSE: commit e657543 added a live elapsed timer to the
  composer's poll loop referencing an UNDEFINED `t0` — ReferenceError killed the
  `while` polling loop on its first iteration, right after the "spawning agent"
  notice rendered. `composing` stayed true forever: send button dead, frame
  frozen at "spawning", nothing updated. Server-side transport was never at
  fault (verified: trivial turn streams step_start→text→step_finish, exit 0).
- Fixes in sendComposer: define `t0`; wrap poll loop in try/catch (a loop error
  can no longer permanently disable the composer); handle unknown-job 404
  (`st.error`); show non-zero exit codes as error notice; elapsed timer only
  while pre-first-event (`dispatching… Ns`), turn-complete notice shows total.
- URL STATE TOKENS: selection is now encoded in the location hash
  `#app=<repokey>&wt=<encpath>&sess=<sessionid>` via syncHash()
  (history.replaceState — no history spam) on switchApp/pickWt/openSess/newSess;
  on first /api/state load applyHash() restores repo (by key), worktree (by
  path, falling back to branch name match), and session. Browser refresh now
  returns to the exact app/worktree/session. Round-trip unit-tested in node.
- AUTO SESSION CONTINUITY: /api/send/<job> now returns `sid`; after a new-
  session turn completes the client adopts it (selSess=sid, continue-line
  updated, hash updated) so the next send continues the SAME session instead of
  spawning a fresh one every send. Also removed the events[-80:] server cap —
  it silently desynced the client's `seen` index on long turns (events skipped).
- switchApp/pickWt now clear the transcript + composer continue-line properly.
- Verified live end-to-end via curl (both turns exit 0): new-session turn
  ("Reply with exactly: OK" → "OK"), then continue on the returned sid → model
  recalled the exact first-turn answer. Two-level continue now proven through
  the console transport. PAGE made a raw string (kills the SyntaxWarning from
  the JS `[\w-]+` regex).

## 2026-09-27 (latest) — Thinking surfaced, markdown/code rendering, model picker

- STREAMING REALITY (probed, don't re-litigate): `kilo run --format json` does
  NOT stream token deltas — every part (text, reasoning) is emitted ONCE,
  complete, when the part finishes (verified: 3877-char text = single event at
  +19.5s; 2473-char reasoning = single event at +14.2s). Live token streaming
  requires the serve+SSE path (message.part.updated) — the deferred attach-mode.
  Within a multi-step turn the frame still updates per step/part completion.
- THINKING: `--thinking` flag now passed on every composer turn (without it the
  model's reasoning parts never reach the NDJSON stream at all — verified).
  Reasoning capped at 4000 chars with …[truncated] marker. `_session_messages`
  now returns PER-PART entries (text + reasoning types from the part table), so
  reloading a session shows thinking blocks interleaved user→reasoning→assistant.
- MARKDOWN (hand-rolled, zero-dep, XSS-safe escape-first): mdRender covers
  fenced code blocks (language chip + copy button via navigator.clipboard),
  inline code, bold, asterisk-italic, http(s) links (noopener, javascript: URLs
  never hyperlinked), h3-h5 headings, ul/ol lists, blockquotes (CommonMark lazy
  continuation), hr, pipe tables with \| escapes. Applied to assistant +
  thinking bubbles (streamed and history); user bubbles stay plain pre-wrap.
  Unit-tested in node against XSS/script-injection samples (all escaped).
- BLOCKQUOTE GOTCHA: `>` must be matched as `(&gt;|>)` AFTER esc() — escaping
  runs before line parsing.
- MODEL PICKER: /api/compose-meta now includes `models` — parsed from
  `kilo models` (712 entries, kilo/<provider>/<model> form), cached 600s.
  Composer gets a datalist-backed input (type-to-filter, native, zero-dep),
  default openrouter/z-ai/glm-5.3-flash. /api/send accepts `model`
  (regex-validated) and passes --model for BOTH new and continue sessions.
  VERIFIED LIVE: continue turn with --model kilo/z-ai/glm-5.3-flash on an
  openrouter-created session → exit 0, same sid, model self-identified. Both
  ID forms (openrouter/... and kilo/...) accepted by the CLI.
- Live e2e: thinking turn (reasoning 1017 chars + fenced code + md table in
  text, exit 0) and model-override continue turn both green; /api/session
  shows reasoning rows. No orphaned kilo children after turns (killpg reaps).

## 2026-09-28 — Stream containment, stop/steer, session delete, working animation, persistent server

- OUTAGE ROOT CAUSE (M'Lord's "can't tell if working"): the console server was
  started with session-scoped lifetime and DIED between Steward turns; M'Lord's
  in-flight turn was killed with its process tree (transient assistant row
  removed). Console server now runs as a PERSISTENT background process
  (survives Steward session end). Lesson: `court ui` must never be
  session-scoped again.
- STREAM CONTAINMENT (fixes "responses streaming into different session
  containers"): streaming events no longer append to whatever transcript is
  open. Each turn is a TURN buffer (blocks); renderLive() renders the buffer
  ONLY while viewingTurn() matches (same worktree + session, or the new-session
  view until sid adoption). Switching away mid-turn buffers silently; switching
  back re-renders the full buffer; on completion the view reloads from kilo.db
  (guarded: only if the turn actually ran). Adoption of the new session's sid
  only happens when the user is still viewing the turn's context.
- STOP: /api/stop {job} — job now stores its Popen handle; killpg SIGTERM then
  SIGKILL after 0.7s grace. Client STOP button (red) appears while a turn runs;
  exit -15/-9 with the stopped flag renders "stopped by user", not an error.
  VERIFIED LIVE: long counting turn stopped at 8s → done, exit -15, no orphans.
- STEER (queue): headless per-turn transport cannot inject a prompt mid-turn
  (concurrent `kilo run --session` writers would race) — steering is
  implemented as QUEUEING: while a turn runs, SEND becomes QUEUE; the typed
  message is buffered on the turn and auto-dispatched on completion, continuing
  the turn's ACTUAL session (resolved at finish time via T.sid||T.sess — not at
  queue time, so early queues still attach to the adopted sid; unit-tested).
  Enter-key queues too. Unit harness (node, stubbed fetch/DOM): 15/15 asserts —
  viewingTurn matrix, queue-continues-adopted-sid, stop flow.
- SESSION DELETE: `kilo session delete <id>` CLI discovered — /api/session/
  delete delegates to it (never hand-write SQL against the live 104GB DB).
  Guards: id regex; 409 if any unfinished job targets the session (by sess or
  sid). UI: hover-✕ on each session tab + chatbar ✕ when a session is open;
  confirm() dialog; after delete, selection moves to next session in the
  worktree or new-session state. VERIFIED LIVE: throwaway session created and
  deleted via endpoint; 0 rows remain in kilo.db.
- WORKING ANIMATION: pulsing three-dot indicator in the live "working… Ns"
  notice + a green pulsing "agent working" chip in the chatbar for the duration
  of a turn (also visible when viewing another session, since the chip is
  global). renderLive autoscrolls only when already near the bottom (no more
  scroll yanking during streams).
- Composer refactor: sendComposer → dispatch(wt,sess,agent,model,prompt) +
  finishTurn; send button no longer disabled during turns (it is QUEUE);
  c_cont/tab state updated via openSess on finalize.
