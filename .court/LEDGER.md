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

## 2026-09-28 (later) — Live-turn view no longer wipes chat history (royal bug report)

- REGRESSION from 46dd4ab's scoped live-turn view: on SEND the transcript was
  fully replaced by TURN blocks, so prior chat history vanished during a turn.
- FIX (direct on castle trunk, royal requested, uncommitted): msgHTML(m)
  extracted from renderTranscript; dispatch() snapshots the pre-turn history
  as T.histHTML (msgs.map(msgHTML) — computed once per turn, not per event);
  renderLive() renders T.histHTML then appends TURN blocks below it.
  History stays visible; "working… Ns" + streamed blocks append at the bottom;
  autoscroll still only when already near bottom. Cross-session scoping kept
  (T.histHTML is per-turn state, so switching tabs mid-turn cannot duplicate
  DB-persisted partial content). Server restarted as persistent background
  proc bgp_0e0cfb765001IkCxDJ8PqNTnRC (pid 82832); verified histHTML served
  + /api/state healthy (200 sessions / 9 worktrees).
- Note: uncommitted on castle trunk: court/ui_server.py + this ledger entry.

## 2026-09-28 (later still) — "turn failed (exit 1) · 23s" forensics; reader hardened

- Royal report 23:09: console turn dispatched ~23:08:41 exited 1 after ~23s.
  Forensics: the spawned `kilo run` produced ZERO stdout, never created its
  per-run log file (successful runs create one within ~1s of boot), wrote
  nothing to kilo.db (no session row, no messages), no macOS crash report.
  Identical command re-verified healthy right after (probe turn, exit 0,
  ses_f1f238429ffe0v8RqsTdFnACQC). opencode.log (27.6GB) was truncated to
  13KB ~23:11 destroying pre-failure history; rotated chunks cover only old
  per-run logs. Root cause UNRECOVERABLE — characteristics (silent 23s then
  exit 1, no boot log) most consistent with a transient startup stall
  (network/system), not console logic. Disk NOT full (72Gi free on Data).
- Console reader hardened (same file): non-JSON process output lines are now
  retained (rolling last ~5, 200 chars each); on not-connected the
  "agent exited before responding" event now ALWAYS fires (with
  "(no output at all)" when empty — the old `raw_tail` truthiness guard hid
  exactly this failure class); on connected+noise emits "process output
  tail: …". Future silent exits render their cause as a red error block.
- Server pid confusion fixed: bgp-reported pid was the wrapper; real python
  (82873, started 23:01:47, pre-reader-patch) survived the wrapper kill and
  held 8300. Killed it; fresh persistent bgp_0e0e696eb0013QOoa1crk014s9
  (pid 3128, started 23:26:43) serving both patches — verified histHTML in
  page + /api/state OK. Old stale bgp entry (bgp_0e0cfb765001IkCxDJ8PqNTnRC,
  pid 82832) is dead wrapper, safe to ignore.

## 2026-09-28 (latest) — ROOT CAUSE of "exit 1" turns: headless permission denial

- Failed console turns (run logs 031540, 032803, 032808, 032919) all show:
  kilo boot OK → stream OK → the steward's bash call `python3 -m court.cli
  raze Q691` permission-REJECTED in 37ms ("The user rejected permission to
  use this specific tool call") → turn.close → process exit 1 (~10-19s).
  Console dispatches are HEADLESS `kilo run` with no interactive approver;
  bash patterns not covered by agent allow rules resolve to ask → instant
  deny. The pb-app steward's "environment issue" was this permission wall;
  its ordered retry loop could never succeed. (23:08:41 silence remains a
  distinct unexplained transient; every later exit-1 had the deny cause.)
- FIX: ui_server._start_run now passes `--auto` to kilo run (auto-approve
  not-explicitly-denied permissions; deny rules still honored). Verified
  end-to-end: headless bash-tool turn with --auto exits 0, tool output
  returned, zero error parts.
- Server restarted: bgp_0e0ec160e001RIL1w68UmCEWt6 (pid 9045).
- Follow-up option (narrower than --auto): add bash allow rules for court
  CLI/git patterns to agent definitions (permissions resolve from agent
  config per opencode.log `action.source=agent`); keep --auto meanwhile.

## 2026-09-27 (04:xx) — Console v0.9: royal review round; scroll/thinking/composer fixed; board + slash commands + ctx/cost shipped

M'Lord directive: serious pre-v1 review of the UI platform + (a) pulsing green
dot on quest cards, (b) thinking auto-expand + broken expand click, (c) status
dashboard with app-tag filtering, (d) slash commands mirroring VS Code, (e)
context-window + cost metrics. Mid-build bug reports: stream scroll snapping,
QUEUE state leaking across session switches, Enter-queue silent drop.

### Root causes + fixes (direct on castle trunk, court/ui_server.py)
- SCROLL SNAP: renderLive rebuilt the whole transcript innerHTML every 700ms
  poll — DOM destruction reset the browser scroll anchor each rebuild.
  FIX: incremental rendering — append-only blocks, persistent spinner element
  updated in place, dataset.turn guard rebuilds once after any foreign render,
  autoscroll still near-bottom only.
- THINKING: was auto-collapsed >700 chars and the expand click was wiped by the
  next full rebuild. FIX: auto-EXPANDED by default; click-to-collapse persisted
  via foldMemo keyed by foldKey(text) + data-fk attrs; survives re-renders and
  DB reloads.
- COMPOSER STATE LEAK: composing/TURN were global — switching sessions while a
  turn ran kept QUEUE + live chip; Enter-queue before job-id assignment was
  silently dropped and invisible when not viewing the turn. FIX: turns[] list +
  turnForView() (per viewed wt/sess); parallel turns across worktrees allowed;
  syncComposer() re-derives SEND/QUEUE/STOP/chip on every state change; queue
  allowed pre-job-id and always renders a visible "queued next" block.
- GREEN DOTS (royal item): /api/state now exposes `active` — DERIVED FROM PS
  (kilo run --dir/--agent regex), so dots on trunk/worktree/board cards are
  truthful for console AND court-CLI dispatches/goads (verified live: external
  pb-app steward detected). Console jobs union in for the pre-spawn window.
- BOARD (royal item): #board view (header CHAT/BOARD vtabs) — kanban columns
  per STATUS_ORDER, app-tag filter chips (tag = frontmatter app or Q-id
  segment), quest cards (id/title/branch/dirty/dot) click-through to the
  worktree in chat view; server /api/state.quests = per-repo .court/{quests,
  epics} scan with TTL caches (_QUESTS_CACHE/_wt_dirty). 87 pb-app quests live.
- SLASH COMMANDS (royal item): /api/commands?dir= lists <dir>/.kilo/{commands,
  command} + global ~/.config/kilo; composer "/" autocomplete (#cmdlist,
  Tab/Arrow/Enter) — prompt sent verbatim, server _expand_command substitutes
  $ARGUMENTS (or appends) and routes agent per command frontmatter (validated
  against allowlist). Unit-checked: /status --tree, /goad Q123, unknown-cmd 400.
- CTX/COST (royal item): session tokens are CUMULATIVE in kilo.db — true
  context = last step-finish part tokens.total. New /api/session/meta?id=
  returns ctx + model + session cost; chatbar shows "ctx 96k/200k (48%) · $x"
  (client ctxLim heuristic by model family); tab tooltips show cumulative
  tok + cost. Dead PAGE.replace("${json.dumps(STATUS_ORDER)}") replaced with
  working __STATUS_ORDER__ injection.
- Stray empty file `{}` at repo root removed.

### Verification
node --check on extracted PAGE JS (clean); module import clean; live curl:
/api/state (repos/quests/active/sessions+meta), /api/commands (34), /api/
session/meta; persistent server restarted twice cleanly (now pid 62338,
bgp_0e0ec160e001RIL1w68UmCEWt6).

### In flight / next
- Three background review agents launched (backend, frontend/UX, product
  gaps); product review returned: TOP finding was the ps-truth dot gap (fixed
  in this round). Pending adoption: board cards fed by `court status --json`
  (attention signals), /api/court action buttons (goad/coin/advance/collect),
  persistent turn journal JSONL, process/reap panel restore, turn-complete
  notifications, session search. Triage after remaining reviews land.
- Review agents: ses_f1ef1d07affeOk8ppyrxfPCoeW (product, done),
  ses_f1ef1d07effeXDx7UaRhGxAVtO (frontend/UX), ses_f1ef3b9d3ffeFurNGbhvbuI3No
  (backend) — results to be triaged into next console round.

## 2026-09-27 (04:4x) — Console hardening: all review P0s + cheap P1/P2s adopted

Both remaining review agents completed (after one connection-reset resume
each). All three P0s from the backend review and both P0s from frontend/UX
review fixed this round, direct on trunk (committed with the batch below).

### Backend (from backend review)
- P0 job-id race: `_send` read `_JOB_SEQ[0]` twice — concurrent sends
  overwrote `_JOBS["N"]` and orphaned the loser's running process (unstopable,
  unpollable). Fixed: id computed once into a local.
- P0 unbounded meta scan: `/api/session/meta` ran `json_extract` over every
  part of the session every 5s poll on the 100GB DB. Fixed: bounded two-step
  query (newest 30 messages' parts). Verified ~10ms live.
- P0 `_reap`: unguarded SIGTERM + uncoerced pid → fixed (int coercion, both
  kills guarded).
- kilo binary path was hardcoded to extension 7.8.1 (breaks on VS Code
  auto-update) → `_kilo_bin()` resolver (latest by mtime across
  kilocode.kilo-code-*, PATH fallback); `_models` now single-flight background
  refresh with stale-serve (was a blocking 60s subprocess on cache miss).
- `_worktrees` non-blocking lock miss returned `[]` (nav/board flash empty) →
  blocking acquire with double-check.
- `_quests` cold-cache serial git-status fan → ThreadPoolExecutor(8); quest
  `worktree:` frontmatter now joined against repo root when relative;
  `_all_quests` returns dict copies (no cache mutation).
- `_mcp_toggle_write`: atomic tmp+os.replace write, per-path lock, `disabled`
  key normalization (enabled/disabled no longer conflict).
- `_expand_command`: unclosed frontmatter no longer leaks frontmatter keys
  into the prompt; expansion now runs AFTER directory validation.
- Event polling: `/api/send/<id>?since=N` cursor + `total` (kills O(n²)
  full-list reserialization per 700ms poll; client updated in lockstep);
  done jobs pruned from `_JOBS` after 10 min (`ended` timestamp added).
- `_read_body` 1MB cap; `_ps_procs` per-line ValueError guard; single-arg
  `os.path.join` cleanup.

### Frontend (from frontend/UX review)
- P0 board clipping: `.bcol` columns were wrapped in an indefinite-height
  flex div → `max-height:100%` collapsed, bottom cards unreachable. Wrapper
  removed; columns scroll independently again.
- P0 injection: dynamic values were interpolated into `'…'` JS string
  literals inside inline onclick attrs (quest frontmatter is AGENT-AUTHORED →
  prompt-injected Serf could break out and spawn agent runs / delete
  sessions from the console origin). All dynamic handlers converted to
  `data-*` attributes + one delegated listener per container (nav, sess tabs,
  board bar/cols, cmdlist, mcp modal); remaining inline handlers audited
  static. `esc()` now also escapes `'` and strips NUL (mdRender placeholder
  regex guarded against crafted NUL lines too).
- P1: `turnForView` now scans most-recent-first (second parallel turn was
  invisible/unstoppable); `openSess`/`loadCmds` stale-response seq guards
  (fast tab B no longer overwritten by slow tab A); `.app` styles unscoped so
  CHAT/BOARD toggle is visibly stateful; composer textarea autosizes
  (46–140px); Enter with no worktree now shows a red hint instead of
  silently dying; cmdlist arrow-key NaN guards; deleted session removed from
  `S.sessions` immediately (tab no longer lingers); STOP latch resets on
  failed stop request and hides until job exists; malformed hash no longer
  kills the poll loop; chatbar ✕ delete button actually toggles; Escape
  closes cmdlist (and MCP modal).

### Verification
node --check clean; import clean; live: page 200, state (87 quests, 14
ps-derived active agents, 200 sessions), commands 34, meta 68ms wall
(incl. curl). Server restarted persistent (pid 3885, bgp_0e0ec160e001
RIL1w68UmCEWt6).

### Deferred (noted, not adopted this round)
- since-cursor alternative: last-good snapshot for transient DB errors; stop
  race pre-spawn window; hung-turn watchdog; route parsing via urlparse;
  delegated listener for modal backdrop/Escape focus management; a11y pass
  (roles/aria/reduced-motion); board fed by `court status --json` attention
  signals; /api/court action buttons (goad/coin/advance); persistent turn
  journal JSONL; process/reap panel UI; turn-complete notifications; session
  search; today-cost rollups. Ranked adoption next rounds.

## 2026-09-27 (10:4x) — Console round 2 (royal assent): board attention signals, action runner, turn journal, procs panel, notifications, search, $today

All five queued items shipped. Direct on trunk.

- PB-APP ENGINE BUG (fixed in BOTH copies): `court status --json` crashed —
  `cmd_status` read `audit.worktree` but WardAudit defines `worktree_path`.
  pb-app commit 91a20c83a (castle trunk, only that hunk — an unrelated
  in-flight raze try/except in the working tree was left alone);
  kilo-castle cli.py fixed same line (latent, would crash identically).
  pb-app/court is a SYMLINK to .court/engine — engine is the tracked source.
- BOARD ATTENTION: `_court_audit(root, ttl=120)` — background-refreshed
  subprocess `python3 -m court.cli status --json` per repo (pb-app first run
  ~13s, cached; stale-serve pattern), merged into /api/state quests as
  `audit` {tasks_done/total/pct, tribute_present, violations, warnings,
  pending_audience, forced_transition, commutation_done, serf_session_id}.
  Board cards: red outline + red id for attention (viol/audience/forced);
  chips (tasks x/y, tribute ✓, viol count, audience/forced/commuted); board
  bar "N need attention". Live: 87/87 audited, 32 attention.
- ACTION RUNNER: POST /api/court {op,id,status?,note?} — strict whitelist
  (goad/coin/collect/raze/dispatch via _COURT_OP_ARITY fixed argv templates +
  advance with status validated against STATUS_ORDER); quest id resolved
  server-side against the quest map (exact or unique prefix; unknown→error
  event); runs with cwd=quest's repo root; goad/coin/dispatch have NO timeout
  (they wrap whole agent turns — /api/stop is the kill path); short ops 180s.
  Streams stdout into the job event buffer; watchJob() renders a live job
  console modal (same /api/send/<job>?since polling); court-op completions
  journaled + quest/audit caches invalidated. Negative paths verified: bad op
  403, bad status 403, unknown id → in-stream error. Card buttons per status:
  WORKING→goad, TRIBUTE_READY→coin+advance(GATE), GATE→collect,
  READY_TO_RAZE→raze, PLANNED→dispatch --standup; all confirm() first.
- TURN JOURNAL: ~/.local/share/kilo-castle/console_turns.jsonl (2MB rotate to
  .1); one line per completed console turn (ts, dir, agent, model, sid, exit,
  duration, prompt_head, error_tail) and court op (op, id, exit). GET
  /api/turns → ≡ header button modal, fail rows red, click-through to session.
- PROCS PANEL: header "N flagged" chip now opens the reap table (pid/rss/
  etime/args + REAP per row → /api/reap). Backend finally has its UI back.
- NOTIFICATIONS: finishTurn — if turn finished while not viewing or tab
  hidden: document.title flash (✓/✕, 8s) + Notification (permission asked on
  first dispatch); focus resets title.
- SESSION SEARCH: ⌕ header button → modal, /api/sessions?q= LIKE over
  title/directory/agent (limit 40, read-only); row click jumps
  repo+worktree+session (jumpToSession).
- $TODAY: /api/state.today = {sessions, cost, tokens} since local midnight;
  header chip. Live: $4.64 / 9.75M tokens / 55 sessions.
- Modal shell generalized (openModal/closeModal; MCP/procs/turns/search
  share it); backdrop-click + Escape close. Reduced-motion media guard.
- Units: journal roundtrip, today totals, search, _find_quest_repo
  (Q602→pb-app root; ambiguous/unknown→None) all green; node --check clean;
  server restarted persistent (pid 79386).

### Deferred for round 3 (if wanted)
- a11y pass beyond Escape/backdrop (roles/aria-live/reduced-motion done only
  for pulse); hung-turn watchdog; last-good snapshot on transient DB errors;
  board epic grouping; job console for dispatch streaming serf.log tail.

## 2026-09-27 (11:4x) — Context limit truth: real per-model windows from OpenRouter catalog (royal bug: "193.4k/195.3k (99%)")

M'Lord's chatbar showed 99% of a 200k cap on a glm-5.3-flash session. Two
defects: (1) `ktop()` divided tokens by 1024 — "193.4k" was actually ~198k
tokens with a misleading label; (2) the 200k cap was a model-FAMILY GUESS —
no context-window metadata exists anywhere in the local stack (kilo.db has no
model table; `kilo models` outputs bare ids; session metadata holds only
sandbox flags).

- FIX: `_ctx_limits_bg()` fetches OpenRouter's public /api/v1/models
  (stdlib urllib, 24h cache, stale-serve, no auth needed) → 458 models with
  real context_length. `_model_ctx_limit()` normalizes openrouter//kilo/~
  prefixes, exact match, then ±prefix alias match (claude-x-latest), then
  family fallback. Exposed as `ctx_limit` in /api/session/meta; client uses
  it before the heuristic.
- REAL LIMITS (catalog): glm-5.3-flash = 1,310,720 (6.5× the guess);
  claude-sonnet-4.5 = 1,000,000; gemini-2.5-pro = 1,048,576; gpt-5 = 400,000.
  The reported session is actually ~175k/1.31M ≈ 13%, not 99%.
- `ktop` now decimal (k=1000, M=1e6). First ~5s after server start serves the
  fallback until the catalog lands (corrects on next poll).
- Verified live: meta now returns ctx_limit 1310720 for the glm session.
  Committed with ledger.

## 2026-09-27 13:24Z — pb-app root session prune (the real "100+ on castle" backlog)

Royal report "still seeing over 100 sessions on castle": console trunk card renders
sessionsFor(repo root) over the 200 most-recent sessions GLOBALLY — the castle card
itself caps at 22 (kilo-castle DB total: 23 sessions, 0 stale — already clean from
this morning's prune). The 100+ surface is the **pb-app `castle` trunk card**:
pb-app ROOT directory held 3,556 sessions (historic serfs/gatekeepers/MoCs/stewards
all parked at root), 3,325 stale >48h, **0 referenced in pb-app .court/.kilo
paperwork**, 231 fresh protected (incl. the live pb-app steward session, 1.1h).

Execution: `kilo session delete` (single-id yargs contract verified — batch form
prints help, deletes nothing). Pass 1 xargs -P12 (10-min tool cap): 668 ok, 206
contention failures. Persistent self-healing worker bgp_0e31b006900152KJFKlBHjRfHi
(/var/folders/ry/qr45fv891c5bjk1ydwzh9bbr0000gn/T/kilo/pb_prune_loop.sh): recomputes
the stale set each round (newest-stale-first so the trunk card empties visibly
fast), xargs -P12, up to 30 rounds, DONE marker when remaining=0.
Protections: 48h predicate keeps everything active paperwork cites; fresh pb-app
steward/console sessions untouched; kilo-castle quest/ledger references checked
both sides. Console card updates on its normal poll — no restart needed.

## 2026-09-27 12:38Z — Castle session prune + VACUUM completed (royal directive, dry-run gated)

ROYAL PRUNE (>48h inactivity, castle branch): survey first caught a UNIT BUG —
kilo.db session timestamps are MILLISECONDS (first pass read them as seconds and
reported "0 stale"); corrected before any delete. Castle-root: 40 sessions →
20 stale >48h (7.5–19.4d old), 20 fresh protected (in-flight agents, probes,
ledger-cited work). Reference check: 0 of the 20 stale ids appear anywhere in
.court/ or .kilo/. Executed `kilo session delete` ×20: deleted=20 failed=0;
verified 0 rows remain, castle-root 40→20, total 5561→5541. (Gotcha: zsh does
not word-split unquoted vars — first loop passed all ids as ONE argv, failed
cleanly, no effect; rerun line-per-id.)

VACUUM: the earlier attempt (started ~11:xxZ) DIED session-scoped with no log,
process, or temp trace — same failure class as the console outage. Relaunched
persistent bgp_0e2e707a20014MibPC3zFxTpig → **done in 390s: kilo.db 104G →
13.88 GB, freelist 0.00 GB** (~90G reclaimed). Snapshot dir (7.5G) is
kilo-managed with NO mapping table in kilo.db — left alone. Standing guidance
unchanged: re-run `court.db_prune vacuum --apply` (persistent lifetime!) when
file exceeds ~2× live.

## 2026-09-27 (late) — Royal Easel v2: shared browser, annotations, vision artist (assent given)

M'Lord assented to the artist-studio upgrade plan. Three probes run first (all PASS):
- VISION: headless `kilo run -f <png> -m openrouter/google/gemini-3.7-flash` read a
  PIL-rendered image exactly ("EASEL PROBE 42 / ROYAL BANNER VERIFICATION") — image
  attachments work headless; `models.artist_vision` pinned to gemini-3.7-flash.
- CDP: branded Chrome 154 accepts --remote-debugging-port + dedicated user-data-dir.
- MCP: `chrome-devtools-mcp@latest` supports `--browserUrl http://127.0.0.1:<port>`.

Design ratified: ONE managed headed Chromium with dedicated persistent profile
(~/.local/share/kilo-castle/studio-chrome-profile — NOT M'Lord's personal profile;
M'Lord logs into the dev app once, auth persists across studios); artist attaches
via chrome-devtools MCP; court-owned injector pins an annotation overlay into every
page via CDP (Page.addScriptToEvaluateOnNewDocument); M'Lord pins selector+note
margin notes; annotations append to <studio-worktree>/.kilo/studio-annotations.jsonl;
console (8300) gains /api/annotation (CORS) + /api/annotations + easel chip/modal.
`court artist-say <qid> "<instruction>"` = headless nudge to the live artist session.
Studio brief template gains Shared Browser + Royal Annotations sections
({{ browser_mcp_line }}, {{ annotations_file }}, {{ vision_model }}).

IN FLIGHT (background subagents, no commits yet; I review+commit on completion):
- EASEL agent ses_f1d30c4baffehI6ZhEUs66utqi: court/browser.py (browser manager,
  stdlib WS client injector), court/assets/annotator.js, cli.py (browser verbs,
  studio auto-start + .worktree-browser, artist-say), MCP config-location probe
  (.kilo/kilo.json vs root kilo.json — root-only would NOT be wired; tracked-file
  pollution at sync-back).
- CONSOLE agent ses_f1d302568ffewsnOm7DGnDwN6C: ui_server.py /api/annotation,
  /api/annotations, easel chip + annotations modal, /api/state easel key.
  DONE 12:33Z — 26/26 endpoint checks green on temp port; live 8300 restarted by
  another session (pid 97200) and NOW SERVES the endpoints (verified: 200 + CORS
  204). Committed 3faac63 (ui_server.py, carries prior in-flight trunk work) +
  5c0ff31 (db_prune.py, the royal-assented cleanup tool).
- BACK-OUT agent ses_f1d228381ffeEfAfHeUzLTqqEu (dispatched on CONSOLE completion):
  message recall → composer refill → resend, CONTINUE vs --fork branch toggle,
  fork-sid adoption (viewing-guarded), live throwaway-session fork probe.
Direct-edit ownership split avoids collisions: EASEL owns cli.py/browser.py/assets,
CONSOLE owns ui_server.py, Steward owns config/templates/kilo.json/ledger.

NEXT on their completion: review diffs, commit, restart persistent console server
(8300) to activate the new endpoints, then first real studio run exercises the
whole chain (browser + MCP attach + annotations + vision screenshots).

### Follow-up royal request (12:27Z): message back-out / recall & resend

M'Lord wants: recall a sent message, put its text back into the chat box, edit,
resend. Transport facts probed: `kilo session` has NO revert subcommand (list/
delete only); `kilo run --fork` EXISTS (fork the session before continuing;
requires --continue/--session). Design:
- Back (⤺) button on user message rows → exact text refills composer, editable.
- SEND continues the SAME session (model sees original + correction — right for
  typo/direction fixes).
- Composer FORK toggle ("branch on send"): when on with a session targeted,
  /api/send adds `--fork` → turn runs on a fresh copy, console adopts the
  returned fork sid (existing adoption path), original session preserved
  untouched. Closest sanctioned thing to rewind on the headless transport.
- True history truncation is NOT offered headless — serve/attach-mode follow-up
  if ever wanted.
QUEUED behind CONSOLE agent (same file, ui_server.py); dispatch on its completion
notification. Scope: ui_server.py only (server fork pass-through + back-out UI +
fork toggle + queue-rides-along semantics).

### Royal session-prune + vacuum relaunch (12:38Z)

- Survey correction: kilo.db session.time_updated is MILLISECONDS — the first
  survey divided as seconds and wrongly reported 0 stale. Corrected: castle ROOT
  directory had 40 sessions, 20 stale >48h (7.5–19.4d), 0 referenced anywhere in
  .court/ or .kilo/ (deterministic grep gate). Deleted all 20 via
  `kilo session delete` (sanctioned CLI; live serve unaffected): 20/20 OK,
  0 rows remain for those ids, castle-root now 20 (all fresh, incl. in-flight
  subagent + console probe sessions). zsh gotcha: unquoted $VAR does NOT
  word-split — first delete attempt was one garbage id (clean failure, no harm).
- VACUUM from the earlier cleanup round NEVER completed (104G still on disk,
  90.01G reclaimable freelist, live ~13.96G; dead process, no log/temp leftovers —
  it was session-scoped, same death class as the console outage). Relaunched
  PERSISTENT: bgp_0e2e707a20014MibPC3zFxTpig (pid 22925,
  `python3 -m court.db_prune vacuum --apply`). Headroom OK (52.7G free vs ~14G
  live image). Contention: kilo serve pid 66641 holds a connection; db_prune
  retries on busy; subagent kilo probe turns may interleave. Expect file
  104G → ~14G after completion; re-check with court.db_prune report.
- Snapshot dir (7.5G) is kilo-managed; no mapping table in kilo.db → no
  deterministic orphan check → left untouched.
- Prune doctrine for the future: guard ms-vs-s in any staleness math; scope
  deletion to directory-scoped candidates + repo-text reference gate.

## 2026-09-27 — kilo.db cleanup: 104G file → ~11G live (royal assent given)

- M'Lord requested a review: "kilo db is like 11gb". Actual: `kilo.db` was
  104G on disk with only ~10.4G live (24.5M of 27.2M pages on freelist —
  SQLite never self-shrinks, `auto_vacuum=0`, WAL mode). Live split: events
  7.3G (all ≤12 days old — replay log), parts 952M, messages 914M.
- Court safety: court reads only `session.directory` (cli.py:161-215) for
  worktree→session lookups; all active worktree sessions are Sep 1+, and the
  Court's durable state is `.court/` markdown — pruning old sessions/DB work
  cannot break the pipeline.
- Executed with assent (no backup retention wanted):
  1. Deleted orphaned snapshot dirs `snapshot/89172e13…` (4.0G, zero sessions)
     and `snapshot/06cfac7e…` (162M, rentalgrid worktree gone) = ~4.2G.
  2. Deleted `kilo_keep_20260926.db` (2.2G — the Sep 1+ court-role transcript
     archive made by the keep-backup tool; M'Lord: "no need to retain backup").
  3. Deleted 18 log files >30d and 3 tool-output files >14d (incl. q209
     tribute scratch; no `.court` references to q209 remain).
- VACUUM via `python3 -m court.db_prune vacuum --apply` (untracked tool from
  the earlier session; review before first use — it is dry-run by default,
  checkpoints WAL per chunk, preflights headroom). Run as a persistent
  background process with retry-on-busy (kilo serve PID 66641 + pb-app
  steward PID 22344 hold live connections). Expected: file 104G → ~10-11G,
  ~94G reclaimed.
- Standing guidance: freelist regrows with event/part churn — re-run
  `court.db_prune vacuum --apply` when the file exceeds ~2× live size
  (`court.db_prune report` shows live vs freelist), with Kilo quiescent.
- NOT committed (other session's in-flight work): `court/ui_server.py` mods,
  `court/db_prune.py` itself, stray `{}` file at repo root.

## 2026-09-27 22:55Z — BUG: easel annotator panel buttons dead (remediation dispatched)

M'Lord reported: the browser annotation modal ("Add a note") renders but its
Cancel / "Save note" buttons do nothing. Root cause (diagnosed, not hand-fixed):
`court/assets/annotator.js` wires the panel node listeners at TOP LEVEL
(`ui.cancelBtn.addEventListener(…)`) right after `buildUi()`, but pinned
scripts (`Page.addScriptToEvaluateOnNewDocument`) run BEFORE `<body>` exists,
so `buildUi()` defers via its retry timer and returns with `ui.cancelBtn`
undefined → TypeError swallowed by the outer catch → Cancel/Save/textarea
wiring never attaches. Document-level listeners (lines 503-505) attach before
the throw, which is why the badge/hover/panel-open all work while the buttons
are dead. First pin via `Runtime.evaluate` into a live document worked;
every subsequent navigation/new document hits the deferred path → dead.
- Fix: move node wiring into `buildUi()`'s mount-success path (both sync and
  retry-timer paths), guarded against double-wiring.
- Remediation dispatched to background subagent ses_f1ae62447ffeGca7gzx7y6MaCh
  (annotator.js only, no commit; node --check + stubbed-DOM harness proof of
  both mount paths required before Steward review/commit).
- Activation after the fix lands: injector reads annotator.js ONCE at injector
  start, and old pinned scripts stay registered per page target — restarting
  the injector alone is NOT enough. Full stop + relaunch required:
  `python3 -m court.cli browser stop` then `browser start --annotate <wt>`
  (or the console launch button). Persistent profile survives; open tabs lost.

### 23:23Z — RESOLVED: fix verified end-to-end on the real chain; committed 8926e5d

- Remediation subagent fixed annotator.js (wireUi() inside buildUi mount path,
  wired-guard; top-level wiring removed) and proved it with a stubbed-DOM node
  harness: baseline reproduced the dead buttons, fixed file ALL PASS on both
  mount paths (deferred + immediate).
- Steward real-chain E2E (throwaway headless Chrome, CDP
  addScriptToEvaluateOnNewDocument → navigate → trusted input events):
  PASS — deferred-path mount, Ctrl+Shift+A mode toggle, panel opens with
  correct meta, Cancel closes (0 POSTs), Save posts payload (selector #bigbtn,
  note intact) and closes panel, Esc exits (badge "1 saved"). Harness:
  /var/folders/.../T/kilo/annotator_e2e.py. Harness gotchas (for future CDP
  probes): DOM.getDocument needs {"pierce":true,"depth":-1} (default depth=1
  is shallow); class lives in the attributes array, not a className key;
  scope node searches to shadowRoots or page elements pollute matches; fixed
  elements can be offscreen in headless (default viewport < window-size flag)
  — click from real pierced box coords, plus --window-size.
- Live probe of M'Lord's browser (CDP 9335, read-only): unified-orders tab
  still pinned with the OLD script (injector 42400 started 19:04, caches the
  script at start) — hence "still doesn't work" at 23:07 was expected until
  browser stop+start. Activation pending M'Lord's go (tabs close; login
  persists).

## 2026-09-28 03:00Z — ENGINE BUG FIXED: `court ui` held the court write lock for its lifetime; Q001 studio-close chartered

- **Bug**: the console server (`python3 -m court.cli ui --port 8300`) bootstraps
  through `main()`'s unconditional `court_write_lock()` and `ui` was absent from
  `_READ_ONLY_COMMANDS` — the server acquired the exclusive flock at boot
  (9:57PM) and held it until death, deadlocking EVERY kilo-castle root-level
  mutation (`court new`, `edict`, `set-section`, ...; pb-app unaffected — its
  invocations lock pb-app's own lock file). Diagnosed via lsof (holder pid =
  server pid 70858). **Fix** (one-line, commit 26a4d0d, castle trunk): add `ui`
  to `_READ_ONLY_COMMANDS`; the server's in-process court ops already serialize
  via short-lived subprocesses. Stale server killed, console restarted as
  PERSISTENT bgp_0e5f460fe001jDGIkHZSusKLxZ (pid 20411) — verified: no lock
  held at boot, console 200. NOTE: `court/ui_server.py` has uncommitted
  in-flight edits from another session left untouched (52+/10-; parses clean).
- **Q001-Castle-Studio-Close-Lifecycle** chartered (PLANNED, Feature) from
  M'Lord's studio-close plot: `court studio <ids> --close` owns the back half
  of the studio lifecycle — five guards (sign-off proof via dated ledger
  marker/`--signoff`; merge-base drift ≤100 commits w/ `--force-union`;
  convoy-race refusal on cogship stamps / `the-gatehouse/*` ancestry; labeled
  cherry-pick extraction instead of branch-merge; conflict → artist union
  brief + UNION-PENDING), close-out manifest at
  `.court/studio-close/<slug>/manifest.md` gating teardown (all-green or
  `--override-manifest`), teardown = runserver kill → worktree to Ashes
  (session alive) → session stop, branch ref kept. Guards 2+3 also arm
  `--sync-back`. Atelier close support = follow-up. Next: dispatch serf.
- **03:35Z ROYAL PIVOT — NO QUEST, NO CASTLE-ON-CASTLE, DIRECT BUILD ON MAIN.**
  The Q001 serf died instantly anyway (headless permission auto-reject, zero
  commits — the known exit-1 class).   Quest file deleted (a6b45dd), worktree +
  branch torn down. The Steward implements `court studio --close` DIRECTLY on
  main per M'Lord's directive; this ledger entry stands as the pivot record.
- **03:5xZ DIRECT BUILD SHIPPED — `court studio <ids> --close`** (commits on
  main, no quest ceremony):
  - New engine module `court/studio_close.py` + `--close` wiring in `cli.py`
    (`cmd_studio` hook + parser flags) + `.kilo/commands/studio-close.md` thin
    wrapper. Five guards: (1) dated `studio sign-off` ledger-marker proof or
    `--signoff "note"` written at invocation; (2) merge-base base-drift vs
    `--drift-threshold`/`studio.close_max_base_drift` (default 100, a
    configured 0 is honored — falsy-or-default bug avoided), re-cut
    recommended, `--force-union` overrides; (3) convoy-race refusal (stamped
    cogship with live `the-gatehouse/<id>` branch, or quest tip contained in
    any `the-gatehouse/*` branch); (4) cherry-pick extraction of labeled
    artist commits (`style(...): <QID> ... royal review ...` subject +
    `Addendum-Quests:` trailer) with `-x` provenance, conflict-abort (never
    auto-union), `git cherry` patch-id already-present verification; (5)
    conflict → machine union brief at
    `.court/studio-close/<slug>/<qid>-union-brief.md` (conflict regions +
    branch-vs-studio feature diffs both directions) + `Studio Close:
    UNION-PENDING` ledger mark + optional `--artist-session`.
  - Close-out manifest `.court/studio-close/<slug>/manifest.md` committed with
    paperwork: per-quest synced (hashes) / union-pending / already-present /
    race-blocked + approved-UI-present YES/NO (patch-id applied or labeled
    files byte-identical between studio tip and branch tip). Teardown gated on
    ALL-GREEN or `--override-manifest`: runserver kill (manage_servers.sh /
    port-file lsof fallback) → AM move-to-Ashes-then-stop sequence (emitted
    for the Steward's agent_manager tool; headless CLI artist processes are
    killed directly) → branch ref kept, deletion manual.
  - `--sync-back` retains as the guarded low-level primitive: same base-drift
    + convoy-race guards now arm it.
  - Tests: new `tests/test_studio_close.py` (22 tests, REAL temp git repos —
    guards, extraction, conflict abort, brief, manifest, teardown, sync-back
    guard refusals); full suite 233 passed. Also repaired 5 PRE-EXISTING
    stale-model test failures (tests/test_studio.py ×2, test_atelier.py,
    test_court_artist.py ×2 hardcoded `glm-5.3` expectations vs config's
    flash) to assert against `config.get_model("artist")` — config is the
    Q455 source of truth.
  - Live smoke: `--help` renders all flags; unknown-id error path clean. No
    real studio touched (pb-app studios live at 8250/8251 under active royal
    review).
- **13:2xZ court ui — day/time on chat messages** (M'Lord request): per-message
  timestamp footer (day · time) on user/assistant/reasoning bubbles, time next
  to the role label, day dividers between calendar days (incl. leading divider
  for the conversation's start day). History from kilo.db `time_created`
  (epoch ms, already in /api/session); live blocks stamp at first render;
  notices/tool/error rows untimestamped. Verified via node --check + behavior
  harness on the touched functions + live page smoke on :8300. NOTE: the
  change landed inside commit 40b541e ("card turn states") — a parallel
  session committed ui_server.py seconds before this session's commit and
  swept the shared working tree; the feature is shipped in HEAD, attributed
  here. Suite green (233). Console serving it: pid 17897 (kilo-wrapper owned,
  took :8300 in a bind race with the session's own restart — the session's
  persistent instance lost the race and is stopped; console healthy).
