# 🏰 Kilo Castle

**Deterministic multi-agent orchestration framework for [Kilo Code](https://kilo.codes) — Git worktrees, strict role separation, model tiering, and durable event-sourced state.**

---

## Contents

1. [Overview](#overview)
2. [The State Model — Durable, Event-Sourced, Conflict-Free](#the-state-model)
3. [The Court — Roles & Model Tiering](#the-court--roles--model-tiering)
4. [Branch Topology & Ephemeral Gatehouse Convoys](#branch-topology--ephemeral-gatehouse-convoys)
5. [The Quest Lifecycle & Lifecycle Gates](#the-quest-lifecycle--lifecycle-gates)
6. [The Council (/plot)](#the-council-plot)
7. [The Scout Reconnaissance Pipeline](#the-scout-reconnaissance-pipeline)
8. [The Tribute Contract & Intelligence Rollups](#the-tribute-contract--intelligence-rollups)
9. [Commutation — The Post-Deployment Pass](#commutation--the-post-deployment-pass)
10. [Protocol Guardrails](#protocol-guardrails)
11. [Quickstart](#quickstart)
12. [Command Reference](#command-reference)
13. [License](#license)

---

## Overview

**Kilo Castle** (also known as **The Court**) is an open-source orchestration layer built on Kilo Code and its Agent Manager. It turns single-agent coding into a disciplined, fleet-coordinated engineering organization — replacing fragile chat history with durable on-disk state, replacing ad-hoc agent sessions with a fixed role hierarchy, and replacing guesswork about "what was actually done" with deterministic, audited handoffs.

The framework's design pillars:

1. **Durable Event-Sourced Persistence** — every Quest/Epic lives in `.court/` as an append-only `.events.jsonl` event log (`merge=union` in `.gitattributes`) with a regenerated `.md` human view. Parallel worktrees merge conflict-free, and state costs zero token burn to read.
2. **Role Hierarchy & Model Tiering** — six first-class agent modes (Steward, Serf, Scout, Master of Coin, Court Artist, Gatekeeper), each pinned to an explicit model and prompt in `kilo.json`.
3. **The Tribute Contract** — every worker renders a mandatory structured report to disk (Ballad, Tribute, Tally, Penance, Audience, Humble Opinion) before review.
4. **Isolated Git Worktree Topology** — tree-structured branches (`quest/<id>-<slug>`, `scout/<id>-<slug>`, `epic/<id>-<slug>`) flow through ephemeral gatehouse convoy branches (`the-gatehouse/<cogship_id> → castle → main`).
5. **Deterministic Lifecycle Gates** — five enforced gates from charter to teardown, each with a clean-tree rule, an audit pass, and an explicit promotion.
6. **Protocol Guardrails** — Castle Guard branch protections, Charter Immutability (anti-tampering), and Zero Roleplay Leakage enforcement keep both the repo and the agents honest.
7. **Commutation Completion Tracking** — post-deploy activation steps are recorded once with `court commute`, then drop out of the outstanding list into a durable done state.

---

## The State Model

Kilo Castle is **stateless in conversation, stateful on disk**. An LLM session may end; the Court's records never do.

```
.court/
├── quests/            active + completed Quests (Q001-*.md + Q001-*.events.jsonl)
├── epics/             Epic Quests (kind: epic)
├── archive/           Quests moved out of the active ledger
├── templates/         dispatch prompt templates (serf, scout, master_of_coin, gatekeeper, artist)
├── engine/            vendored engine copy for consumer repositories (see `court update`)
├── config.json        model tiering + runtime config
├── LEDGER.md          durable running log of Steward decisions & Audience records
└── README.md
```

- **Event-sourced**: `<id>.events.jsonl` is the source of truth — an append-only log of field/section changes. `.md` files are regenerated views. `merge=union` makes concurrent edits across worktrees merge without conflicts.
- **Zero token burn to read**: `court status`, agent sessions, and dispatchers all read disk, never chat history.

Consumer repositories (like an app repo using the Court) keep the framework in sync with one command:

```bash
court update      # vendors engine/, templates/, commands/, agents/, prompts/, kilo.json, AGENTS.md
```

---

## The Court — Roles & Model Tiering

```
👑 M'Lord (Human Owner)
   │
   ▼
🏰 The Steward (resides on castle — Gemini 3.7 Flash)
   │
   ├──► 🌲 Scout (scout/* branches — GLM 5.3 Flash)
   │        5-Part Scout Report → /plot → production Quests
   │
   ├──► 🔨 Serf (quest/* worktrees — GLM 5.3 Flash)
   │        implements Quests against the charter
   │
   ├──► 🪙 Master of Coin (TRIBUTE_READY audit — Gemini 3.8 Flash)
   │        verifies claims live, settles paperwork, names Commutation
   │
   ├──► 🎨 Court Artist (UI studio with live runserver — GLM 5.3)
   │        refines front-end with M'Lord before collection
   │
   └──► 🛡️ Gatekeeper (the-gatehouse/* convoys — Gemini 3.8 Flash)
          unified test run → promote to castle
                   │
                   ▼
             🏰 castle (staging) ──► 🚀 main (production)
```

| Role | Where it runs | Responsibilities | Model (pinned) |
|---|---|---|---|
| **👑 M'Lord** | Human | Product authority, scope changes, irreversible decisions, Audience rulings. | Human |
| **🏰 Steward** | `castle` (primary agent) | Orchestrator, strategic planner, Council (`/plot`), dispatch, triage, teardowns. Never runs test suites — that belongs to the gatehouse. | **Gemini 3.7 Flash** (`openrouter/google/gemini-3.7-flash`) |
| **🔨 Serf** | Quest worktree (`quest/*`) | Disposable coding agent implementing one Quest. Builds production-ready code against the immutable charter, renders Tribute. Never orchestrates. | **GLM 5.3 Flash** (`openrouter/z-ai/glm-5.3-flash`) |
| **🌲 Scout** | `scout/*` branch (Investigation lane) | POC reconnaissance: probes APIs, feasibility, throwaway scripts in `tasks/artifacts/`, renders the 5-part Scout Report. Branch is razed, never merged. | **GLM 5.3 Flash** (`openrouter/z-ai/glm-5.3-flash`) |
| **🪙 Master of Coin** | Dedicated session in Quest worktree at `TRIBUTE_READY` | Audits Expected vs Delivered against the `castle` baseline charter, verifies claims live, settles paperwork, names Commutation steps, syncs verdict back (`git push . HEAD:<branch>`), advances to `GATE`. No code work. | **Gemini 3.8 Flash** (`openrouter/google/gemini-3.8-flash`) |
| **🎨 Court Artist** | Quest worktree with live runserver | Interactive UI studio: preview, critique, polish templates/views/styles directly with M'Lord after MoC audit, before Gatekeeper collection. | **GLM 5.3** (`openrouter/z-ai/glm-5.3`) |
| **🛡️ Gatekeeper** | Ephemeral `the-gatehouse/<cogship_id>` convoy | Packs MoC-approved Quests, runs the unified integration suite across the pack, isolates/rejects failures via `/reject_tribute`, promotes the clean convoy directly to `castle`, packs the ship manifest. | **Gemini 3.8 Flash** (`openrouter/google/gemini-3.8-flash`) |
| **🏹 Warden** | Patrol (`court ward`) | Diagnostic patrol of production logs/error trackers; files 5-part Warden Reports (Survey, Stack Trace, Impact, Root Cause, Proposed Fix) to be chartered as `Bug fix` Quests. Diagnosis only — never remediates. | /ward command |

Agent definitions live in **three synced places**: `kilo.json` (`agent` map — the live configuration), `.kilo/agent/` + `.kilo/agents/` (installed agent files), and `court/agents/` (source templates). Every summonable role carries an explicit `model`, `provider`, and prompt — agents are never summoned inheriting whatever model the active turn happens to use.

---

## Branch Topology & Ephemeral Gatehouse Convoys

```
main                    (production root trunk)
  ^
castle                  (staging root trunk for main; attached to localhost)
  ^
  Gatehouse folder (ephemeral per-convoy staging branches; one per Cog Ship
  convoy, torn down after promotion — never reused):
    - the-gatehouse/<cogship_id>   (e.g. the-gatehouse/cogship-042)
  ^
  Organizational branch folders:
    - Epics folder:               epic/<epic_id>-<slug>
    - Epic child quests folder:   quest/<epic_id>/<quest_id>-<slug>
    - Standalone quests folder:   quest/<quest_id>-<slug>
    - Scout spikes folder:        scout/<quest_id>-<slug> or scout/<epic_id>/<quest_id>-<slug>
```

**Branch naming is non-negotiable** — flat hyphen names (`quest-q062-...`) are rejected. Every branch begins with its folder prefix (`epic/`, `quest/`, `scout/`, `the-gatehouse/`) so Git GUI clients fold the hierarchy cleanly.

### Agent Manager Sections

- **`Bug fix`** — scoped fixes; verified against affected component tests.
- **`Feature`** — net-new production functionality; component + integration tests.
- **`Optimization`** — refactoring, performance, query optimization; full broad suite.
- **`Investigation`** — spikes, POCs, exploratory research (Scouts); **never auto-merges into `gatehouse`**.
- **`GATEHOUSE`** — ephemeral staging worktrees (`the-gatehouse/<cogship_id>`).
- **`Ashes`** — completed worktrees queued for teardown.

### Gatehouse Convoys

Gatehouse branches are **ephemeral and routed by convoy size**:

- **Convoy of size 1** — the Gatekeeper role runs directly inside that Quest's own existing worktree; nothing to batch.
- **Convoy of size > 1** — one brand-new ephemeral worktree is spawned on a fresh `the-gatehouse/<cogship_id>` cut from `castle`'s tip. The Gatekeeper packs the Quest branches, runs the unified suite once, isolates/rejects any failing Quest, promotes the clean remainder into `castle`, then the worktree is torn down.

---

## The Quest Lifecycle & Lifecycle Gates

A Quest moves through a deterministic pipeline. Every transition is logged in its **Castle Ledger** and persisted to the event log.

```
OPEN → PLANNED → CHARTERED → DISPATCHED → WORKING → TRIBUTE_READY → GATE → READY_TO_RAZE → LANDED/LAUNCHED/DONE
                                                              ↘ side-states: HELD, PUNISHED, DEMOTED
```

| Gate | Stage | Enforced rule |
|---|---|---|
| **1. Creation & Spawning** | OPEN → WORKING | Serf persona + explicit GLM 5.3 Flash model, canonical slash branch naming, `git branch -m` + `git merge castle --ff-only` as first commands. |
| **2. Working & Tribute** | WORKING → TRIBUTE_READY | Clean tree, scratch deleted, one deferred rebase (`git merge castle`), zero drift (behind: 0), Tribute rendered, self-advanced. |
| **3. Master of Coin Review** | TRIBUTE_READY → GATE | Dedicated audit session; clean tree + behind: 0; line-by-line audit of Expected vs Delivered against the **baseline charter on `castle`**; scope/extra-tribute/jargon-leakage audit; Commutation named. |
| **4. Gatekeeper Integration** | GATE → castle | Unified integration suite on the convoy worktree, promote to `castle`, advance to READY_TO_RAZE, pack the ship manifest (`court ship`). |
| **5. Raze & Teardown** | READY_TO_RAZE → Ashes | Verify merge ancestry, fast-forward branch, move worktree to Ashes. |

---

## The Council (/plot)

`/plot` is not a spec-writing command — it is **Council**. M'Lord brings an ambition, complaint, opportunity, Scout Report, or half-formed scheme; the Steward plots the realm before any Serf is levied.

```
👑 Intent → 🏰 /plot convenes Council
     ├── surveys the realm (code, docs, tests, existing Quests)
     ├── charts dependent matters (The Decision Tree)
     ├── settles discoverable facts (never asks what it can find)
     ├── brings judgments to Audience (The Frontier — only ripe, unblocked decisions)
     └── conducts Trial by Example (edge cases, error modes, boundaries)
     ↓
📜 The Plot → 👑 royal assent → ✅ Confirmed Plot (OPEN → PLANNED) → charter
```

**Core principles:** survey before asking · bring concrete recommendations (matter, stakes, recommended ruling, alternatives) · private council (1 question at a time) vs full council (1–3 ripe questions) · challenge false names before code · continue until the Decision Tree is exhausted, then seal.

---

## The Scout Reconnaissance Pipeline

```
[1. RECONNAISSANCE]
/scout <app> <concern> "<goal>"
   │
   ▼
🌲 Scout branch (scout/<id>-<slug>, Investigation lane, GLM 5.3 Flash)
   scratch scripts → tasks/artifacts/, API probes, feasibility spikes
   │
   ▼
📜 5-Part Scout Report (durable markdown)
   ├─ 🧭 1. The Survey   — executive viability verdict, discoveries, confidence score
   ├─ 🗺️ 2. The Map      — charted terrain, endpoints, payload schemas, data tiers
   ├─ ⚠️ 3. The Dangers  — rate limits, edge cases, cost traps
   ├─ 🧪 4. The Tribute  — scratch scripts, realistic fixtures, benchmarks
   └─ 📐 5. The Plot     — proposed production architecture & candidate Quests
   │
[2. VALUE EXTRACTION GATE]
M'Lord + Steward: /plot → review report → blueprint production Quest(s)
   │
[3. PRODUCTION IMPLEMENTATION]
🏰 Production Quest (quest/<id>-<slug>) — Serf builds against the blueprint
   Master of Coin audits → Gatekeeper tests on convoy → merge to castle
   (The scout/* branch is razed to Ashes — never merged wholesale into staging)
```

---

## The Tribute Contract & Intelligence Rollups

Before advancing to review, every Serf renders a formal report persisted under `## Tribute Rendered`.

```
                    # Tribute Rendered
        ┌───────┬────────┬────────┬─────────┬─────────┬──────────┐
        ▼       ▼        ▼        ▼         ▼         ▼
    1. Ballad  2. Tribute 3. Tally  4. Penance 5. Audience 6. Humble Opinion
        ▼        ▼        ▼        ▼         ▼         ▼
     📜/bard  💰/coffers 🔍/tally  🪨/atone  👑/audience  👂/murmur
```

| Rollup | Content |
|---|---|
| **📜 `/bard`** | Narrative arc & release story across an Epic or App. |
| **💰 `/coffers`** | Provable deliverables: commits, line diff stats, test exit codes, new endpoints. |
| **🔍 `/tally`** | Production & UI verification runbooks (exact URLs, commands, inputs, expected outcomes). |
| **🪨 `/atone`** | Agent penance — shortcuts, deferred edge cases, confidence ratings → tech debt backlog. |
| **👑 `/audience`** | Pending trade-offs and questions requiring M'Lord's authority. |
| **👂 `/murmur`** | Bottom-up field intelligence — emergent optimizations, next architectural moves. |

Scouts render a 5-part report instead (Survey, Map, Dangers, Tribute, Plot).

---

## Commutation — The Post-Deployment Pass

When a Quest reaches production, the Master of Coin's audit may name **Commutation**: operational steps production still needs to activate value — env var flips, data backfills, worker restarts, setting toggles, manual verification.

The Commutation record has **two halves**:

1. **The instruction** — written once by the Master of Coin under `**Commutation:**` in `## Master of Coin's Audit`. Left untouched, preserving audit history.
2. **The completion marker** — a dated `- **Commutation (YYYY-MM-DD):** …` bullet appended to the Quest's `## Cogship Log` by the Steward after the deploy pass:

```bash
python3 -m court.cli commute Q215 --note "set BRAVE_SEARCH_API_KEY_FREE on pb-app + pb-app-worker; verified ledger table via brave_search_usage."
```

Once logged, the Quest **drops out** of the outstanding lists:

- `court status` splits `⚡ COMMUTATIONS REQUIRED (N)` from a collapsed `✅ Commutations Done (N) — logged in Cogship Log: <ids>`.
- `court ship`'s ⚡ Commutation Manifest carries only pending commutations; done ones are noted on a collapsed line.
- `status --json` exposes `commutation_done` per quest.

`court commute` is idempotent (a completion entry already present → no-op; `--force` appends another). Hand-written Cogship Log entries (e.g. `Commutation #1 (2026-09-08): … ✓`) are recognized as valid completion markers, so the convention works on records logged before the verb existed.

---

## Protocol Guardrails

### Zero Roleplay Leakage

The Court/Castle metaphors — `Tribute`, `Serf`, `Kingdom`, `Castle`, `Court`, `Penance`, `Ballad`, `Cogship`, etc. — exist **strictly as internal orchestration vocabulary**. They must never cross into production artifacts:

- **User-facing UI** (templates, headers, table columns, badges, modals, alerts, form labels)
- **Database schemas** (models, table/column names, migration operations)
- **Application code** (services, functions, view context variables, serializers, tasks)
- **API contracts** (endpoints, query parameters, JSON payloads, error strings)
- **Test suites** (file/class names, assertions)

Charters and prompts are written 100% out of character in plain, domain-accurate engineering language. Both the Master of Coin (Gate 3) and Gatekeeper (Gate 4) scan for leaked jargon; any found is grounds for immediate audit rejection.

### Charter Immutability & Anti-Tampering

- `# The Kingdom Requires` and the text of `# Expected Tribute` items are **strictly immutable** by Serfs.
- A Serf may only toggle checklist status (`- [ ]` → `- [x]`) without altering wording, render its report under `## Tribute Rendered`, and self-advance to `TRIBUTE_READY`.
- Auditors verify against the **baseline charter on `castle`** (`git show castle:.court/quests/<id>.md`), never the worktree's local copy. Any unauthorized change is Charter Tampering — immediate audit failure.

### Castle Guard

`court/ward.py` + `court/castle_guard.py` enforce technical branch protection: no direct commits to staging/production trunks, no bypassed promotion steps, orphaned-worktree detection, and a realm-wide compliance patrol (`court ward`).

### The Pillory

Failed audits are not soft-rejected. A Quest is sent **to the pillory** (`court pillory` / `/pillory`): frozen as `PUNISHED` with Decrees for a chartered successor (`--pillory-of`), its worktree marked read-only. Only side-states; never silently returned to WORKING.

---

## Quickstart

### 1. Install

```bash
pip install kilo-castle
# or
pipx install kilo-castle
# or from a checkout, zero-install:
python3 -m court.cli --help
```

This installs the `court` (and `kilo-castle`) console command.

### 2. Initialize in any repository

```bash
cd /path/to/your/project
court init
```

This creates `.court/`, `.kilo/commands/`, `.kilo/agents/`, `.kilo/prompts/`, `kilo.json` (all six agent modes + models), and `AGENTS.md`. Existing repositories stay current with `court update`.

### 3. Summon agents

Agents are defined in `kilo.json` (`mode: primary`, pinned model, full prompt). Dispatches always pass the model explicitly — never inherit the active turn's model:

- **Serf**: `court dispatch <id> --standup` (GLM 5.3 Flash) or `kilo run --agent serf --model "GLM-5.3-Flash" --dir <worktree>`
- **Scout**: `/scout <app> <concern> "<goal>"` (GLM 5.3 Flash)
- **Master of Coin**: `/levy <id>` starts a fresh dedicated worktree session (Gemini 3.8 Flash)
- **Court Artist**: `/artist <id>` spawns the interactive UI studio with a live runserver (GLM 5.3)
- **Gatekeeper**: `/collect` packs the convoy and prints the Gatekeeper NEXT STEPS (Gemini 3.8 Flash)

### 4. Daily workflow

| Command | Action |
|---|---|
| `/status` | Full Court dashboard (pipeline lanes, dirty/behind states, audiences, commutations) |
| `/plot` | Interactive blueprinting Council: priorities, planning docs, next Quests |
| `/scout <app> <concern> "<goal>"` | Dispatch an exploratory Scout on a `scout/*` branch |
| `/charter <id>` | Commission a Quest (fold notes in, advance OPEN → PLANNED, prepare dispatch) |
| `court dispatch <id> --standup` | Spawn the Serf worktree session (GLM 5.3 Flash) → WORKING |
| `/goad <id>` | Nudge an idle/exited Serf, or diagnose whether it actually finished |
| `/bear-tribute <id>` | Prompt an active Serf to render its 5-part completion report |
| `/levy` | Triage worktrees, sync tribute, route rendered Tributes to the Master of Coin |
| `/audience <id> "..."` | Resolve a pending decision requiring M'Lord's judgment |
| `/artist <id>` | Court Artist UI studio with live runserver (after MoC, before collection) |
| `/collect` | Pack approved Quests (TRIBUTE_READY → GATE) into a Cog Ship convoy |
| `/reject_tribute` | Isolate a failing Quest from a convoy and dispatch Serf remediation |
| `/ship [--confirm]` | Compile the deployment convoy summary + Commutation manifest; `--confirm` deploys |
| `court commute <id> --note "..."` | Record a post-deploy commutation as done (Cogship Log entry) |
| `/pillory <id>` | Freeze a Quest as `PUNISHED` with Decrees for a successor |
| `/raze <id>` | Verify merge, fast-forward branch, move worktree to Ashes |
| `/teardown` | List worktrees ready for M'Lord to prune |
| `/bard` `/coffers` `/tally` `/atone` `/murmur` | Intelligence rollups across the fleet |
| `/edict "<text>"` | Record a strategic decree from M'Lord |
| `/ward` | Warden's log-patrol dashboard and realm compliance health |

---

## Command Reference

**Slash commands** (`.kilo/commands/`, run inside Kilo): `artist` `atone` `audience` `bard` `bear-tribute` `charter` `close` `coffers` `cog` `collect` `dismiss` `edict` `epic` `goad` `hold` `levy` `murmur` `pillory` `plot` `quest` `raze` `reject_tribute` `report-to-the-king` `resume` `return` `scout` `ship` `status` `tally` `teardown` `ward`

**CLI verbs** (`court <verb>` / `python3 -m court.cli <verb>`):

```
advance archive artist audit charter collect commute diff dispatch
dispatch-complete edict fix-branches fork-teardown-list init levy list
log new pillory punish raze realign-branches rebase rollup set-field
set-section ship show stamp status tally teardown-list timber tree update
verify verify-merged ward worktree-doc
```

Highlights: `verify` / `verify-merged` deterministic worktree checks · `rebase` zero-turn branch convergence · `diff` structured worktree diff · `rollup` extract any tribute pillar · `stamp` allocate a Cog Ship id · `timber` cross-reference worktrees ↔ sections ↔ Quests · `archive` shelve closed records · `update` vendor the latest engine.

---

## License

[MIT](LICENSE) © 2026 Jason Davis