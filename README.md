# 🏰 Kilo Castle

**Deterministic multi-agent orchestration framework for [Kilo Code](https://kilo.codes) with Git Worktrees, role separation, and durable event-sourced state.**

---

## Overview

**Kilo Castle** (also known as **The Court**) is an open-source orchestration layer built on top of Kilo Code and VS Code Agent Manager. It turns single-agent coding into a disciplined, fleet-coordinated engineering organization.

Instead of relying on fragile chat history or allowing a single LLM session to wander across an entire codebase, Castle introduces:
1. **Durable Event-Sourced Persistence**: Quests and Epics tracked on disk in `.court/` backed by append-only `.events.jsonl` event logs (`merge=union` in `.gitattributes`) with `.md` as a regenerated human view — conflict-free merges across parallel worktrees with zero token burn to read state.
2. **Role Hierarchy & Model Tiering**: Strict division of labor:
   - **Steward** (Orchestrator resident on `castle`)
   - **Serf** (Disposable implementers in worktrees — **GLM 5.3 Flash**)
   - **Scout** (Reconnaissance on non-merging `scout/*` branches)
   - **Master of Coin** (Accounting arm auditing value in `TRIBUTE_READY` via dedicated worktree session — **Gemini 3.7 Flash**)
   - **Gatekeeper** (Batch integration & test execution at `GATE` on ephemeral convoys — **Gemini 3.7 Flash**)
   - **Warden** (Diagnostic log & error auditor on `ward/*` branches)
3. **The Tribute & Scout Completion Contracts**: Mandatory structured handoffs written durably to disk (Ballad, Tribute, Tally, Penance, Audience, Humble Opinion).
4. **Isolated Git Worktree Topology**: Tree-structured branches (`quest/<id>-<slug>`, `scout/<id>-<slug>`) flowing through ephemeral gatehouse convoy staging branches (`the-gatehouse/<cogship_id>` $\rightarrow$ `castle` $\rightarrow$ `main`).
5. **Castle Guard**: Technical branch-protection rules preventing direct commits or bypasses to staging/production trunks.
6. **The Pillory & Commutation**: Value/scope/duplication rejection freezes a Quest as `PUNISHED` with Decrees for a successor Quest; post-deploy activation requirements are recorded under `Commutation`.

---

## Hierarchy of the Court

```
                     👑 M'Lord (Human Owner)
                               │
                ┌──────────────┴──────────────┐
                ▼                             ▼
       🏰 The Steward                 👑 Audience
  (Orchestrator / Planner)        (Decisions requiring M'Lord)
                │
                ├─────────────────────────────┬─────────────────────────────┐
                ▼                             ▼                             ▼
       🌲 The Scout                  🪙 Master of Coin              🛡️ Gatekeeper
  (POC / Reconnaissance)         (Value & Resource Audit)       (Test Suite & Merge Gate)
        │                                     │                             │
        ▼                                     ▼                             ▼
  Scout Worktrees                     🔨 Serf Worktrees             🛡️ Gatehouse Convoy
 (Throwaway Scripts)              (Disposable Implementers)      (the-gatehouse/<cogship>)
        │                                     │                             │
        └──────────────► /plot ───────────────┴─────────────────────► 🏰 Castle (Staging)
                  (Blueprint Quests)                                        │
                                                                            ▼
                                                                      🚀 Main (Prod)
```

### The Roles

| Role | Responsibilities | Recommended Model Tier |
|---|---|---|
| **👑 M'Lord** | The human owner. The only source of genuine product authority, scope changes, and irreversible decisions. | Human |
| **🏰 Steward** | The primary orchestrator and strategic planner you interact with. Triages tasks into Quests, writes dispatch contracts, monitors active work, blueprints next steps (`/plot`), manages teardowns, and synthesizes fleet rollups. | Fast / Resident (e.g. Gemini 3.7 Flash) |
| **🌲 Scout** | Reconnaissance agent for proof-of-concept investigations on non-merging `scout/*` branches. Probes APIs, tests feasibility with throwaway scripts in `tasks/artifacts/`, and generates the 5-part Scout Report. | Fast / Cost-Efficient (e.g. Gemini 3.7 Flash / GLM 5.3 Flash) |
| **🔨 Serf** | Disposable coding agent assigned to a single isolated Git worktree. Builds clean, production-ready code against approved blueprints. | Fast / Cost-Efficient (**GLM 5.3 Flash** mandate) |
| **🪙 Master of Coin** | Dedicated session inside Quest worktree during `TRIBUTE_READY`. Audits rendered Tribute, verifies claims live, settles paperwork, identifies Commutation activation steps, and syncs verdict back (`git push . HEAD:<branch>`). | Fast / Capable (**Gemini 3.8 Flash**) |
| **🎨 Court Artist** | Interactive UI/UX craftsmanship studio directly with M'Lord. Runs inside Quest worktree with live runserver to preview, critique, and refine front-end templates live before collection. | Aesthetic / Fast (**GLM 5.3**) |
| **🛡️ Gatekeeper** | Ephemeral convoy worktree (`the-gatehouse/<cogship_id>`) during `GATE`. Packs convoy batches, runs unified integration test suites across the pack all at once, isolates/re-tests and rejects failing commits via `/reject_tribute` with Serf remediation, and promotes verified Cog Ships directly into `castle`. | Fast / Capable (**Gemini 3.8 Flash**) |
| **📜 Vassal** | Dispatched for large, multi-Quest **Epics**. Decomposes initiatives into child Quests, coordinates dependencies, and compresses fleet status upward. | Fast / Frontier |
| **🏹 Warden** | Patrols production logs and error trackers on a non-merging `ward/*` branch. Files 5-part Warden Reports (Survey, Stack Trace, Impact, Root Cause Diagnosis, Proposed Fix) for the Steward to charter into `Bug fix` Quests. Diagnoses only — never writes remediation code. | Fast / Cost-Efficient |

---

## The Steward's Council (/plot) — Turning Desire into Confirmed Blueprint

`/plot` is not a command to write a specification — it is **Council**.

M'Lord brings an ambition, complaint, opportunity, Scout Report, or half-formed scheme before the Steward. The Steward does not immediately levy Serfs and hope they interpret the intent correctly. The Steward first plots the realm: discovering what is already known, exposing what remains undecided, and bringing only genuine matters of judgment before M'Lord.

```
👑 Intent
      │
      ▼
🏰 /plot — Steward convenes Council
      │
      ├── surveys the realm (code, docs, tests, existing Quests)
      ├── charts dependent matters (The Decision Tree)
      ├── settles discoverable facts
      ├── brings judgments to Audience (The Frontier)
      └── conducts Trial by Example
      │
      ▼
📜 The Plot — Steward reads the settled understanding
      │
      ▼
👑 M'Lord gives royal assent
      │
      ▼
✅ Confirmed Plot (OPEN -> PLANNED)
      │
      ▼
📜 Quest Remit & Expected Tribute
      │
      ▼
⚔️ Dispatch (/charter -> /dispatch)
```

> **The Council Workflow**:
> **Survey the realm. Convene Council. Hear M'Lord. Confirm the Plot. Then levy the work.**

### Core Council Principles

1. **The Tree & The Audience Frontier**:
   Before questioning M'Lord, the Steward charts the matter as a dependency tree. Dependent matters are never brought to Audience until their prerequisites are settled. Ripe, unblocked decisions form the **Audience Frontier**.
2. **Survey Before Asking**:
   The Steward searches the codebase, docs, tests, git history, and `.court/` before asking M'Lord discoverable repository facts.
3. **Bring Concrete Recommendations**:
   Instead of open-ended asking ("how should this work?"), the Steward presents the matter, the stakes, a recommended ruling (Humble Opinion), grounds, and alternatives.
4. **Private vs. Full Council**:
   - **Private Council**: 1 question at a time (for voice interfaces, high-consequence architecture, or ambiguous initiatives).
   - **Full Council**: 1–3 ripe questions at a time (for terminal/chat, ranked by leverage).
5. **Challenge False Names & Trial by Example**:
   - Ambiguous domain terms are clarified before writing code.
   - Abstract agreements are tested against concrete edge cases, error modes, and boundary conditions before confirmation.
6. **Confirmation & Sealing**:
   Council continues until the Decision Tree is exhausted and no material matter remains in Audience. The Steward presents `# 📜 The Plot` (Intent, Decrees, Bounds of Realm, Findings, Consequences, Delayed Judgments, Victory Criteria). Upon royal assent, the Plot is sealed, transitioning the Quest from `OPEN` $\rightarrow$ `PLANNED`.

---

## The Scout Reconnaissance Pipeline

```
[1. RECONNAISSANCE]
/scout <app> <concern> "<goal>"
       │
       ▼
🌲 Scout Branch (`scout/<id>-<slug>` in Investigation lane)
Agent: The Scout (Spikes, scratch scripts in `tasks/artifacts/`, web fetches, API probes)
       │
       ▼
📜 5-Part Scout Report (Durable Markdown)
   ├─ 🧭 1. The Survey: Executive viability verdict, core discoveries & confidence score
   ├─ 🗺️ 2. The Map: Charted terrain, endpoints, payload schemas & data tiers
   ├─ ⚠️ 3. The Dangers: Minefield map, hidden rate limits, edge cases & cost traps
   ├─ 🧪 4. The Tribute: Scratch scripts, realistic test fixtures & benchmarks
   └─ 📐 5. The Plot: Proposed production service architecture & candidate Quests
       │
[2. VALUE EXTRACTION GATE]
M'Lord + Steward: /plot
Review Scout Report ──► Determine Vision ──► Blueprint Production Quest(s)
       │
       ▼
[3. PRODUCTION IMPLEMENTATION]
🏰 Production Quest (`quest/<id>-<slug>` in Feature / Optimization lane)
Serf builds clean, modular service code strictly against the Scout's Blueprint + Fixtures
Master of Coin Audits ──► Gatekeeper Tests on Gatehouse Convoy ──► Merge to Castle
(The Scout branch is razed to Ashes — never merged wholesale into staging)
```

---

## The Tribute Contract & Intelligence Rollups

Before advancing to review, every Serf and Scout must render a formal report persisted to disk under `# Tribute Rendered`.

```
                    ┌────────────────────────────────────────────────────────┐
                    │               # Tribute Rendered                       │
                    └────────────────────────────────────────────────────────┘
                                                 │
          ┌───────────────────┬──────────────────┼───────────────────┬───────────────────┐
          ▼                   ▼                  ▼                   ▼                   ▼
    1. Ballad            2. Tribute         3. Penance          4. Audience        5. Humble Opinion
          │                   │                  │                   │                   │
          ▼                   ▼                  ▼                   ▼                   ▼
       📜 /bard           💰 /coffers        🪨 /atone          👑 /audience        👂 /murmur
   (Narrative Arc &    (Provable Code &   (Tech Debt & Meta-   (Decisions for      (Bottom-Up Field
    Release Story)       Asset Ledger)     Prompt Feedback)        M'Lord)           Intelligence)
```

1. **📜 `/bard` (Ballad)**: Weaves narrative summaries across an Epic or App into a cohesive executive story.
2. **💰 `/coffers` (Tribute)**: Collects hard deliverables: git commits, line diff stats, test exit codes, new endpoints, and **The Tally** (production & UI verification runbooks).
3. **🪨 `/atone` (Penance)**: Aggregates agent self-flagellations on shortcuts taken, deferred edge cases, and confidence ratings to generate technical debt backlogs.
4. **👑 `/audience` (Audience)**: Surfaces pending trade-offs and questions requiring M'Lord's authority.
5. **👂 `/murmur` (Humble Opinion)**: Clusters bottom-up suggestions from agents in the trenches on emergent optimizations and next architectural moves.

---

## Branch Topology & Ephemeral Gatehouse Convoys

```
main                    (production root trunk)
  ^
castle                  (staging root trunk for main; attached to localhost)
  ^
  Gatehouse folder (Ephemeral per-convoy staging branches; one per Cog Ship convoy, torn down after promotion):
    - the-gatehouse/<cogship_id>   (e.g. the-gatehouse/cogship-042 — brand-new branch per convoy, never reused)
  ^
  Organizational branch folders:
    - Epics folder:               epic/<epic_id>-<slug>
    - Epic child quests folder:   quest/<epic_id>/<quest_id>-<slug>
    - Standalone quests folder:   quest/<quest_id>-<slug>
    - Scout spikes folder:        scout/<quest_id>-<slug> or scout/<epic_id>/<quest_id>-<slug>
```

### Agent Manager Sections

- **`Bug fix`**: Scoped fixes; verified against affected component tests.
- **`Feature`**: Net-new production functionality; verified against component + integration tests.
- **`Optimization`**: Refactoring, performance, query optimization; full broad test suite.
- **`Investigation`**: Spikes, POCs, exploratory research (Scouts); **never auto-merges into `gatehouse`**.
- **`GATEHOUSE`**: Ephemeral staging worktrees (`the-gatehouse/<cogship_id>`).
- **`Ashes`**: Completed worktrees queued for manual teardown.

---

## Quickstart

### 1. Installation

```bash
pip install kilo-castle
# or with pipx:
pipx install kilo-castle
```

### 2. Initialize in Any Repository

```bash
cd /path/to/your/project
court init
```

### 3. Daily Workflow with Kilo Slash Commands

| Command | Action |
|---|---|
| `/plot` | Interactive blueprinting: consults M'Lord on priorities, planning docs, and next Quests |
| `/scout <app> <concern> "<goal>"` | Dispatch an exploratory Scout for POC reconnaissance on a `scout/*` branch |
| `/edict <text>` | Record a strategic decree or priority from M'Lord |
| `/status` | Display Court dashboard (Audiences, In-Review, Active Serfs/Scouts, Teardown queue) |
| `/charter <id>` | Commission a Quest (fold notes into charter, advance to `PLANNED`, prepare dispatch) |
| `/dispatch <id>` | Spawn a Serf worktree session with GLM 5.3 Flash and advance to `WORKING` |
| `/levy` | Scan the Court for idle Serfs, rebase to zero drift, and route rendered Tributes to Master of Coin |
| `/collect` | Pack approved Quests at `GATE` into a Cog Ship convoy for Gatekeeper testing & promotion |
| `/ship` | Compile Cog Ship deployment convoy summary with Commutation manifest |
| `/reject_tribute` | Isolate a failing Quest from a Cog Ship and dispatch Serf remediation in its worktree |
| `/bear-tribute` | Prompt an active Serf to render its 5-part completion report |
| `/bard` | Synthesize narrative ballads across Quests into a release story |
| `/coffers` | Aggregate provable deliverables and production verification runbooks |
| `/tally` | Extract and summarize production verification runbooks across Quests |
| `/atone` | Aggregate agent penance to uncover tech debt and prompt flaws |
| `/murmur` | Surface bottom-up field recommendations from the agents |
| `/audience` | Surface pending decisions requiring M'Lord's judgment |
| `/raze <id>` | Verify merge status, fast-forward branch, and move worktree to Ashes |
| `/goad <id>` | Nudge an idle or exited Serf session, or diagnose whether it actually finished |
| `/pillory <id>` | Freeze a Quest as `PUNISHED` with Decrees for a chartered successor |
| `/ward` | Display the Warden's log-patrol dashboard, or dispatch a fresh patrol session |

---

## License

[MIT](LICENSE) © 2026 Jason Davis
