# 🏰 Kilo Castle

**Deterministic multi-agent orchestration framework for [Kilo Code](https://kilo.codes) with Git Worktrees, role separation, and durable state.**

---

## Overview

**Kilo Castle** (also known as **The Court**) is an open-source orchestration layer built on top of Kilo Code and VS Code Agent Manager. It turns single-agent coding into a disciplined, fleet-coordinated engineering organization.

Instead of relying on fragile chat history or allowing a single LLM session to wander across an entire codebase, Castle introduces:
1. **Durable Markdown Persistence**: Quests and Epics tracked on disk in `.court/` with frontmatter and monotonic global IDs — zero token burn to read state.
2. **Role Hierarchy**: Strict separation between the orchestrator (**Steward**), disposable implementers (**Serfs**), value auditors (**Master of Coin**), and integration checkpoints (**Gatekeeper**).
3. **The Tribute Completion Contract**: Mandatory 5-part structured handoff (**Ballad**, **Tribute**, **Penance**, **Audience**, **Humble Opinion**) written durably to disk.
4. **Tribute Rollup & Intelligence Layer**: Deterministic extraction of fleet progress (`/bard`, `/coffers`), technical debt & prompt feedback (`/atone`), bottom-up field recommendations (`/murmur`), and interactive roadmapping (`/plot`).
5. **Isolated Git Worktree Topology**: Tree-structured branches (`quest/<id>-<slug>`) flowing through staging lanes (`gatehouse` $\rightarrow$ `castle` $\rightarrow$ `main`).
6. **Model Tiering**: Fast, cost-efficient models in the trenches; frontier reasoning models at the gate.

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
                ├─────────────────────────────┐
                ▼                             ▼
       🪙 Master of Coin              🛡️ Gatekeeper
   (Value & Resource Audit)       (Test Suite & Merge Gate)
                │                             │
                ▼                             ▼
        🔨 Serf Worktrees              🏰 Castle (Staging)
     (Disposable Implementers)                 │
                                              ▼
                                         🚀 Main (Prod)
```

### The Roles

| Role | Responsibilities | Default Model Tier |
|---|---|---|
| **👑 M'Lord** | The human owner. The only source of genuine product authority, scope changes, and irreversible decisions. | Human |
| **🏰 Steward** | The primary orchestrator and strategic planner you interact with. Triages tasks into Quests, writes dispatch contracts, monitors active work, blue prints next steps (`/plot`), and synthesizes fleet rollups. | Fast / Resident (e.g. Gemini 3.7 Flash) |
| **🔨 Serf** | Disposable coding agent assigned to a single isolated Git worktree. Does **not** own the worktree — if it hallucinates or stalls, the Steward dismisses it and dispatches a fresh Serf into the *same* worktree. | Fast / Cost-Efficient (e.g. Gemini 3.7 Flash) |
| **🪙 Master of Coin** | Dispatched during `REVIEW` **before** testing and merging. Audits the rendered Tribute directly on the worktree for criteria satisfaction, scope discipline, and query/compute cost efficiency. | Fast / Cost-Efficient (e.g. Gemini 3.7 Flash) |
| **🛡️ Gatekeeper** | Lives inside the persistent `gatehouse` integration worktree. Dispatched during `GATE`. Independently re-verifies the diff, executes the test suite, and merges into `gatehouse` and `castle`. | Frontier Reasoning (e.g. Claude 3.7 Sonnet) |
| **📜 Vassal** | Dispatched only for large, multi-Quest **Epics**. Decomposes initiatives into child Quests, coordinates dependencies, and compresses fleet status upward. | Fast / Frontier |

---

## The Quest Lifecycle

```
OPEN ──► PLANNED ──► DISPATCHED ──► WORKING ──► REVIEW (Master of Coin) ──► GATE (Gatekeeper) ──► READY_FOR_TEARDOWN ──► DONE
                       ▲                │         │                             │
                       │                └─────────┼─────────────────────────────┘
                       │                          │  (Rejections return to WORKING)
                       │                          ▼
                       └──────────────────────── HELD (Awaiting Audience Decision)
```

- **`WORKING`**: The Serf writes code and commits exclusively on its tree branch (`quest/q001-slug`).
- **`REVIEW`**: Master of Coin audits the real diff and acceptance checklist. Returns to `WORKING` if incomplete or wasteful.
- **`GATE`**: Gatekeeper executes integration test suites on the `gatehouse` layer and merges into `castle`.
- **`READY_FOR_TEARDOWN`**: Merged worktree is moved to **Ashes** section for manual pruning in Agent Manager.

---

## The Tribute Contract & Intelligence Rollups

A plain "done" from a coding agent is never accepted. Before advancing to review, every Serf must render a formal 5-part report persisted to disk under `# Tribute Rendered`.

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

## Strategic Blueprinting & Roadmapping (`/plot` & `/edict`)

- **`/plot`**: Interactive blueprinting command. The Steward consults M'Lord in a **question-forward** dialogue, synthesizing Royal Edicts, active planning files, backlog items, and recent Serf intelligence (`/atone` and `/murmur`) to propose and scope the next candidate Quests.
- **`/edict`**: Record M'Lord's strategic decrees and high-level priorities directly into `.court/EDICTS.md` to guide future blueprinting.

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
```

### Agent Manager Sections

- **`Bug fix`**: Scoped fixes; verified against affected component tests.
- **`Feature`**: Net-new functionality; verified against component + integration tests.
- **`Optimization`**: Refactoring, performance, query optimization; full broad test suite.
- **`Investigation`**: Spikes, POCs, exploratory research; never auto-merges into `gatehouse`.
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

This automatically scaffolds:
- `.court/` (ledger, quest store, and dispatch prompt templates)
- `.court/EDICTS.md` (royal decrees store)
- `.kilo/commands/` (`/charter`, `/levy`, `/collect`, `/raze`, `/gate`, `/review`, `/bear-tribute`, `/audience`, `/status`, `/bard`, `/coffers`, `/atone`, `/murmur`, `/plot`, `/edict`)
- `.kilo/prompts/` (`steward.md`, `master_of_coin.md`, `gatekeeper.md`)
- `AGENTS.md` (canonical branch topology and division of labor instructions)

### 3. Daily Workflow with Kilo Slash Commands

Inside Kilo Code, interact naturally with the Steward:

| Command | Action |
|---|---|
| `/plot` | Interactive blueprinting: consults M'Lord on priorities, planning docs, and next Quests |
| `/edict <text>` | Record a strategic decree or priority from M'Lord |
| `/status` | Display Court dashboard (Audiences, In-Review, Active Serfs, Teardown queue) |
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

## Deterministic CLI Reference

The `court` CLI is 100% Python standard library with zero external runtime dependencies:

```bash
# Create a new Quest
court new --app api --concern auth-jwt-rotation \
  --title "Rotate JWT secret keys" \
  --section "Bug fix" \
  --goal "Fix token rotation race condition" \
  --tribute "- [ ] All auth tests pass"

# Rollup & Intelligence extraction
court rollup --section ballad --epic Q012    # Extract ballads across an Epic
court rollup --section tribute --status DONE # Extract deliverables for done Quests
court rollup --section penance --all         # Extract technical debt & skipped items
court rollup --section opinion               # Extract field recommendations

# Royal Edicts
court edict "Prioritize worker resilience and reduce database compute hours"
court edict                                  # View active decrees

# Inspect and manage
court status                                 # Show Court state dashboard
court list                                   # List all active Quests
court show Q001                              # View full Quest markdown
court advance Q001 WORKING                   # Transition pipeline status
court set-field Q001 branch "fix/jwt"         # Update frontmatter field
court set-section Q001 "Expected Tribute" --file /tmp/tribute.md
court teardown-list                          # Show worktrees ready to prune
court archive Q001                           # Move Quest to archive
```

---

## Contributing & Development

```bash
git clone https://github.com/jasonhdavis/kilo-castle.git
cd kilo-castle
pip install -e ".[dev]"
pytest
```

---

## License

[MIT](LICENSE) © 2026 Jason Davis
