# 🏰 Kilo Castle

**Deterministic multi-agent orchestration framework for [Kilo Code](https://kilo.codes) with Git Worktrees, role separation, and durable state.**

---

## Overview

**Kilo Castle** (also known as **The Court**) is an open-source orchestration layer built on top of Kilo Code and VS Code Agent Manager. It turns single-agent coding into a disciplined, fleet-coordinated engineering organization.

Instead of relying on fragile chat history or allowing a single LLM session to wander across an entire codebase, Castle introduces:
1. **Durable Markdown Persistence**: Quests and Epics tracked on disk in `.court/` with frontmatter and monotonic global IDs — zero token burn to read state.
2. **Role Hierarchy**: Strict separation between the orchestrator (**Steward**), disposable implementers (**Serfs**), value auditors (**Master of Coin**), and integration checkpoints (**Gatekeeper**).
3. **The Tribute Completion Contract**: Mandatory 5-part structured handoff (**Ballad**, **Tribute**, **Penance**, **Audience**, **Humble Opinion**) written durably to disk.
4. **Isolated Git Worktree Topology**: Tree-structured branches (`quest/<id>-<slug>`) flowing through staging lanes (`gatehouse` $\rightarrow$ `castle` $\rightarrow$ `main`).
5. **Model Tiering**: Fast, cost-efficient models in the trenches; frontier reasoning models at the gate.

---

## Hierarchy of the Court

```
                     👑 M'Lord (Human Owner)
                               │
                ┌──────────────┴──────────────┐
                ▼                             ▼
       🏰 The Steward                 👑 Audience
  (Orchestrator / Observer)       (Decisions requiring M'Lord)
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
| **🏰 Steward** | The primary orchestrator and observer agent you interact with. Triages tasks into Quests, writes dispatch contracts, monitors active work, and routes completions. | Fast / Resident (e.g. Gemini 3.7 Flash) |
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

## The Tribute Contract (Report to the King)

A plain "done" from a coding agent is never accepted. Before advancing to review, every Serf must render a formal 5-part report persisted to disk:

1. **Ballad**: Narrative summary of work accomplished, problem context uncovered, and architectural choices made.
2. **Tribute**: Provable work product delivered:
   - Changed/created files with diff stats.
   - Git commit hashes on branch.
   - Exact test commands run and literal exit codes/tail output.
   - Produced artifacts, endpoints, or data payloads.
3. **Penance**: Honest agent self-flagellation on what was half-done, deferred, shortcuts taken, or where confidence is shaky.
4. **Audience**: Explicit requests for decisions or authority required from M'Lord.
5. **Humble Opinion**: The agent's concrete recommendations for next steps.

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
- `.kilo/commands/` (`/charter`, `/levy`, `/collect`, `/raze`, `/gate`, `/review`, `/bear-tribute`, `/audience`, `/status`)
- `.kilo/prompts/` (`steward.md`, `master_of_coin.md`, `gatekeeper.md`)
- `AGENTS.md` (canonical branch topology and division of labor instructions)

### 3. Daily Workflow with Kilo Slash Commands

Inside Kilo Code, interact naturally with the Steward:

| Command | Action |
|---|---|
| `/status` | Display Court dashboard (Audiences, In-Review, Active Serfs, Teardown queue) |
| `/charter <id>` | Commission a Quest (create worktree, tree branch, section lane, dispatch Serf) |
| `/levy` | Scan the Court for idle Serfs and route rendered Tributes to Master of Coin |
| `/review <id>` | Dispatch Master of Coin to audit value and resource efficiency |
| `/collect` | Collect approved Tributes, run tests on `gatehouse`, and merge to `castle` |
| `/gate <id>` | Dispatch Gatekeeper to test and merge a specific Quest |
| `/bear-tribute` | Prompt an active Serf to render its 5-part completion report |
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

# Inspect and manage
court status                         # Show Court state dashboard
court list                           # List all active Quests
court show Q001                      # View full Quest markdown
court advance Q001 WORKING           # Transition pipeline status
court set-field Q001 branch "fix/jwt" # Update frontmatter field
court set-section Q001 "Expected Tribute" --file /tmp/tribute.md
court teardown-list                  # Show worktrees ready to prune
court archive Q001                   # Move Quest to archive
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
