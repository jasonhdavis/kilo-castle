# 🏰 Kilo Castle

**Deterministic multi-agent orchestration framework for [Kilo Code](https://kilo.codes) with Git Worktrees, role separation, and durable state.**

---

## Overview

**Kilo Castle** (also known as **The Court**) is an open-source orchestration layer built on top of Kilo Code and VS Code Agent Manager. It turns single-agent coding into a disciplined, fleet-coordinated engineering organization.

Instead of relying on fragile chat history or allowing a single LLM session to wander across an entire codebase, Castle introduces:
1. **Durable Markdown Persistence**: Quests and Epics tracked on disk in `.court/` with frontmatter and monotonic global IDs — zero token burn to read state.
2. **Role Hierarchy**: Strict separation between the orchestrator (**Steward**), disposable implementers (**Serfs**), reconnaissance explorers (**Scouts**), value auditors (**Master of Coin**), and integration checkpoints (**Gatekeeper**).
3. **The Tribute & Scout Completion Contracts**: Mandatory structured handoffs written durably to disk.
4. **Isolated Git Worktree Topology**: Tree-structured branches (`quest/<id>-<slug>`, `scout/<id>-<slug>`) flowing through staging lanes (`gatehouse` $\rightarrow$ `castle` $\rightarrow$ `main`).
5. **Model Tiering**: Fast, cost-efficient models in the trenches; frontier reasoning models at the gate.

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
  Scout Worktrees                     🔨 Serf Worktrees              🏰 Castle (Staging)
 (Throwaway Scripts)              (Disposable Implementers)                 │
        │                                                                   ▼
        └──────────────► /plot ─────────────────────────────────────►  🚀 Main (Prod)
                  (Blueprint Quests)
```

### The Roles

| Role | Responsibilities | Default Model Tier |
|---|---|---|
| **👑 M'Lord** | The human owner. The only source of genuine product authority, scope changes, and irreversible decisions. | Human |
| **🏰 Steward** | The primary orchestrator and strategic planner you interact with. Triages tasks into Quests, writes dispatch contracts, monitors active work, blue prints next steps (`/plot`), and synthesizes fleet rollups. | Fast / Resident (e.g. Gemini 3.7 Flash) |
| **🌲 Scout** | Reconnaissance agent for proof-of-concept investigations on non-merging `scout/*` branches. Probes APIs, tests feasibility with throwaway scripts in `tasks/artifacts/`, and generates the 5-part Scout Report. | Fast / Cost-Efficient (e.g. Gemini 3.7 Flash) |
| **🔨 Serf** | Disposable coding agent assigned to a single isolated Git worktree. Builds clean, production-ready code against approved blueprints. Dismissed and replaced if confused. | Fast / Cost-Efficient (e.g. Gemini 3.7 Flash) |
| **🪙 Master of Coin** | Dispatched during `REVIEW` **before** testing and merging. Audits the rendered Tribute directly on the worktree for criteria satisfaction, scope discipline, and query/compute cost efficiency. | Fast / Cost-Efficient (e.g. Gemini 3.7 Flash) |
| **🛡️ Gatekeeper** | Lives inside the persistent `gatehouse` integration worktree. Dispatched during `GATE`. Independently re-verifies the diff, executes the test suite, and merges into `gatehouse` and `castle`. | Frontier Reasoning (e.g. Claude 3.7 Sonnet) |
| **📜 Vassal** | Dispatched only for large, multi-Quest **Epics**. Decomposes initiatives into child Quests, coordinates dependencies, and compresses fleet status upward. | Fast / Frontier |

---

## The Scout Reconnaissance Pipeline (Pioneering Methods Without Polluting Production)

The transition from a Proof-of-Concept to production code is often painful when agents try to weld exploratory scripts directly into core services. Castle enforces a strict separation:

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
Master of Coin Audits ──► Gatekeeper Tests on Gatehouse ──► Merge to Castle
(The Scout branch is razed to Ashes — never merged wholesale into staging)
```

---

## The Tribute Contract & Intelligence Rollups

A plain "done" from a coding agent is never accepted. Before advancing to review, every Serf and Scout must render a formal report persisted to disk under `# Tribute Rendered`.

Each section maps directly to dedicated slash commands and CLI rollups:

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

1. **📜 `/bard` (Ballad)**: Weaves narrative summaries across an Epic or App into a cohesive executive story of challenges encountered, architecture decisions, and transformed features.
2. **💰 `/coffers` (Tribute)**: Collects the hard deliverables across Quests: git commits, line diff stats, passed test exit codes, new endpoints, and data payloads.
3. **🪨 `/atone` (Penance)**: Aggregates agent self-flagellations on shortcuts taken, deferred edge cases, and shaky confidence to automatically generate technical debt backlogs and prompt/rule improvement items.
4. **👑 `/audience` (Audience)**: Surfaces pending trade-offs and questions requiring M'Lord's authority.
5. **👂 `/murmur` (Humble Opinion)**: Clusters bottom-up suggestions from agents in the trenches on emergent optimizations and next architectural moves.

---

## Branch Topology & Agent Manager Lanes

```
main                    (production)
  ^
castle                  (staging for main; attached to localhost)
  ^
gatehouse               (persistent rolling integration branch; test execution layer)
  ^
  agent worktrees, structured in tree format:
    - epics:              epic/<epic_id>-<slug>
    - epic child quests:  quest/<epic_id>/<quest_id>-<slug>
    - standalone quests:  quest/<quest_id>-<slug>
    - scout spikes:       scout/<quest_id>-<slug> (non-merging exploratory POCs)
```

### Agent Manager Sections

- **`Bug fix`**: Scoped fixes; verified against affected component tests.
- **`Feature`**: Net-new production functionality; verified against component + integration tests.
- **`Optimization`**: Refactoring, performance, query optimization; full broad test suite.
- **`Investigation`**: Spikes, POCs, exploratory research (Scouts); **never auto-merges into `gatehouse`**.
- **`GATEHOUSE`**: Staging merge target worktree.
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

Inside Kilo Code, interact naturally with the Steward:

| Command | Action |
|---|---|
| `/plot` | Interactive blueprinting: consults M'Lord on priorities, planning docs, and next Quests |
| `/scout <app> <concern> "<goal>"` | Dispatch an exploratory Scout for POC reconnaissance on a `scout/*` branch |
| `/edict <text>` | Record a strategic decree or priority from M'Lord |
| `/status` | Display Court dashboard (Audiences, In-Review, Active Serfs/Scouts, Teardown queue) |
| `/charter <id>` | Commission a Quest (create worktree, tree branch, section lane, dispatch Serf) |
| `/levy` | Scan the Court for idle Serfs and route rendered Tributes to Master of Coin |
| `/review <id>` | Dispatch Master of Coin to audit value and resource efficiency |
| `/collect` | Collect approved Tributes, run tests on `gatehouse`, and merge to `castle` |
| `/gate <id>` | Dispatch Gatekeeper to test and merge a specific Quest |
| `/bear-tribute` | Prompt an active Serf to render its 5-part completion report |
| `/bard` | Synthesize narrative ballads across Quests into a release story |
| `/coffers` | Aggregate provable deliverables (commits, line counts, endpoints, passed tests) |
| `/atone` | Aggregate agent penance to uncover tech debt and prompt flaws |
| `/murmur` | Surface bottom-up field recommendations from the agents |
| `/audience` | Surface pending decisions requiring M'Lord's judgment |
| `/raze <id>` | Move a merged worktree to Ashes for teardown |

---

## License

[MIT](LICENSE) © 2026 Jason Davis
