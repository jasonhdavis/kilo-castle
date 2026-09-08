# Agent Instructions & Branch Topology — Kilo Castle

This file is loaded automatically for every agent session (Kilo CLI, Agent Manager worktrees,
and any subagents) in a repository using Kilo Castle.

---

## Branch Topology & Git Organizational Folders (Branch Namespaces)

Git GUI clients (such as VS Code Source Control / Git Graph, GitHub, Sublime Merge, Tower) group branches into collapsible **organizational folders** using forward slashes (`/`). Branches named with flat hyphens fail to fold and pollute the root namespace.

**All branches MUST use forward-slash (`/`) folder hierarchy without exception:**

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

### Canonical Branch Folder Naming Reference:

| Branch Kind | Organizational Folder Pattern | Example |
|---|---|---|
| **Epic** | `epic/<epic_id>-<slug>` | `epic/q050-platform-task-execution-v2-api-security` |
| **Epic Child Quest** | `quest/<epic_id>/<quest_id>-<slug>` | `quest/q050/q051-tasks-unified-enqueue-contract` |
| **Standalone Quest** | `quest/<quest_id>-<slug>` | `quest/q069-platform-high-volume-context-queries` |
| **Scout Spike (POC)** | `scout/<quest_id>-<slug>` | `scout/q074-shops-product-runbook-workflow-spike` |
| **Core Trunks** | Root names (no folder) | `main`, `castle` |
| **Gatehouse Convoy** | `the-gatehouse/<cogship_id>` | `the-gatehouse/cogship-042` (ephemeral — one brand-new branch per convoy, torn down after promotion; never a fixed/reused name) |

**FORBIDDEN**: Flat hyphens like `quest-q062-...`. Every Quest, Epic, Scout, and Gatehouse branch must begin with its proper folder prefix (`epic/`, `quest/`, `scout/`, or `the-gatehouse/`).

---

## Ephemeral Gatehouse Convoys & Direct-Promotion Pipeline

To maximize throughput and prevent bottlenecks or double-gating, gatehouse staging branches are **ephemeral and routed by convoy size**:

1. **Convoy of size 1**: run the Gatekeeper role directly inside that one Quest's own existing worktree/session — nothing to batch, so no separate integration point is spawned.
2. **Convoy of size > 1**: spawn **one brand-new ephemeral Agent Manager worktree** scoped to just that convoy, on branch `the-gatehouse/<cogship_id>`, cut from `castle`'s current tip. The Gatekeeper packs the convoy's Quest branches into it, runs the unified integration suite once across the pack, isolates/rejects any failing Quest, then promotes the clean remainder directly into `castle`. The worktree is torn down (Ashes/stop) once promoted — **never** attach to an old/idle named station and never reuse a convoy branch name.

---

## Division of Labor

- **Serf on Quest Worktrees (Never Steward)**: Quests MUST ALWAYS be spawned and executed using a **Serf** worker with **GLM 5.3 Flash** (`model: "GLM-5.3-Flash"` / `provider: "openrouter"` / `openrouter/z-ai/glm-5.3-flash`), **NEVER a Steward**. The Steward resides on `castle`. Worktrees belong strictly to disposable Serf workers who implement the assigned Goal & Scope without orchestration authority or subagent permissions.
- **Master of Coin (Dedicated Session; Broad Authority, Narrow Remit)**: The Master of Coin is the Court's administrative/accounting arm. It MUST ALWAYS evaluate inside the Quest's existing worktree/branch via a **brand-new, dedicated Agent Manager session** bound to that exact existing branch (`model: "Gemini 3.8 Flash"` / `openrouter/google/gemini-3.8-flash`). **Broad authority**: it may run live verification commands directly in the worktree to confirm Tribute claims, and it repairs/settles the Charter's paperwork. **Narrow remit**: it does no coding work — no code fixes, no touching the diff. Its job ends at exactly one Pass/Fail verdict plus Recommended Next Steps. **Commutation duty**: every audit names what production still needs to do to activate value (deploy commands, backfills, env var flips, migrations) under `Commutation`. **Sync-back**: the Master of Coin must always push its verdict back to the real branch (`git push . HEAD:<real-branch>`) before reporting done.
- **Gatekeeper on `the-gatehouse/<cogship_id>`**: The Gatekeeper runs inside a **brand-new ephemeral gatehouse convoy worktree** per Cog Ship convoy of size > 1 (or directly in the Quest worktree for size 1). Model: **Gemini 3.8 Flash** (`openrouter/google/gemini-3.8-flash`). Merges candidate branches, executes the unified integration test suite across the pack, isolates/rejects failing Quests via `/reject_tribute` with Serf remediation, promotes verified convoys directly into `castle`, and packs the Cog Ship manifest (`python3 -m court.cli ship`).
- **Court Artist (Interactive UI Studio; Direct Royal Collaboration)**: The Court Artist is the Court's interactive UI/UX craftsman (`model: "GLM-5.3"` / `openrouter/z-ai/glm-5.3`). When a Quest modifies user-facing templates or views, M'Lord summons the Court Artist directly into a dedicated worktree session with an active development server (`court artist <id>`). The Serf is permitted to create the initial UI, but M'Lord and the Court Artist refine typography, colors, and layout live in the browser before collection.
- **Steward on `castle`**: Manages lifecycle, planning, Council (`/plot`), dispatching, triage, and teardowns. Does not run test suites directly on `castle`.

---

## Agent Compliance & Lifecycle Gates

1. **Gate 1 — Creation & Spawning**:
   - Stand up Serf session via Court CLI (`court dispatch <id> --standup`) or Kilo CLI (`kilo worktree create` + `kilo run --agent serf` / API) with GLM 5.3 Flash (`openrouter/z-ai/glm-5.3-flash`).
   - Pure task instructions only: the Serf's persona and constraints live in its first-class system prompt (`.kilo/agent/serf.md` or `kilo.json`). Zero persona prompt boilerplate injected into the turn.
   - Strictly name branch with slash hierarchy: `quest/<id>-<slug>`, `epic/<id>-<slug>`, `scout/<id>-<slug>`.
   - Run `git branch -m <canonical_branch>` and sync with castle (`git merge castle --ff-only`).
2. **Gate 2 — Working & Tribute (`# Tribute Rendered`)**:
   - Stage and commit deliverables. Run `git status --porcelain` to verify clean tree.
   - Perform deferred rebase (`git merge castle`) right before rendering tribute and advancing to `TRIBUTE_READY`. Verify zero drift (`↓0`).
   - Run `python3 -m court.cli advance <id> TRIBUTE_READY --note "Tribute rendered, deferred rebase complete."`.
3. **Gate 3 — Master of Coin Review (`TRIBUTE_READY`)**:
   - Dedicated session in worktree. Verify clean tree and `behind: 0`. Audit line-by-line against Expected Tribute. Settle paperwork, name Commutation, advance to `GATE`, and sync back with `git push . HEAD:<branch>`.
   - Audit for Charter Anti-Tampering, Extra Tribute Not Requested, and Zero Roleplay Leakage.
4. **Gate 4 — Gatekeeper Integration (`GATE`)**:
   - Run unified integration tests on convoy branch, verify zero roleplay leakage in production assets, promote to `castle`, advance Quests to `READY_TO_RAZE`, and pack deployment manifest (`court ship`).
5. **Gate 5 — Raze & Teardown (`READY_TO_RAZE` -> `Ashes`)**:
   - Run `court raze <id>` to verify merge ancestry, fast-forward branch, and move to Ashes. Run `court fork-teardown-list` to move and stop disposable Master of Coin and Gatekeeper scaffolding worktrees.

---

## Zero Roleplay Leakage & Out-of-Character Specifications

The Court and Castle metaphors (`Steward`, `Serf`, `Master of Coin`, `Gatekeeper`, `Warden`, `Vassal`, `Scout`, `Tribute`, `The Kingdom Requires`, `Expected Tribute`, `Ballad`, `Penance`, `Tally`, `Audience`, `Humble Opinion`, `Pillory`, `Decrees`, `Levy`, `Cog Ship`, `Ashes`, etc.) exist **strictly as internal orchestration, bookkeeping, and protocol scaffolding**. They govern agent roles, lifecycle gates, and ledger history.

**They must NEVER cross the boundary into production artifacts:**
1. **Out-of-Character Charters & Prompts**: When the Steward writes Quest Charters (`The Kingdom Requires`, `Expected Tribute`, acceptance criteria) or Serf dispatch prompts, all technical requirements, user stories, acceptance criteria, UI descriptions, and domain logic MUST be written **100% out of character** in plain, professional, domain-accurate engineering language.
2. **Prohibited in Production Code & UI**: Castle/Court roleplay terms (`Tribute`, `Serf`, `Kingdom`, `Castle`, `Court`, `Penance`, `Ballad`, `Tally`, `Pillory`, `Cogship`, etc.) are STRICTLY PROHIBITED in:
   - User-facing UI (templates, HTML headers, table columns, button labels, badge text, modal titles, alert text, form labels, tooltips).
   - Database schemas (models, table names, column/field names, migration operations).
   - Application code (service classes, function names, view context variables, serializers, tasks, decorators).
   - API contracts (endpoints, URLs, query parameters, JSON request/response payloads, error strings).
   - Test suites (test file names, test class names, test assertions).
3. **Audit Enforcement**: The Master of Coin at Gate 3 (`TRIBUTE_READY`) and Gatekeeper at Gate 4 (`GATE`) explicitly scan diffs for jargon leakage. Any internal Court roleplay vocabulary found in production templates, code, or schemas is grounds for **immediate audit rejection and pillory**.

### Charter Immutability & Anti-Tampering Protocol

To prevent Serfs from retroactively expanding their own remit or laundering smuggled scope by amending the charter mid-flight:
1. **Strict Charter Immutability**:
   - The `# The Kingdom Requires` (or `# Goal & Scope`) section and the text of `# Expected Tribute` checklist items are **strictly immutable by Serfs**.
   - A Serf in a worktree is permitted ONLY to:
     - Toggle item completion status (`- [ ]` -> `- [x]`) without altering the item's wording.
     - Render its 5-part completion report under `## Tribute Rendered`.
     - Self-advance the status from `WORKING` to `TRIBUTE_READY`.
   - A Serf is **STRICTLY FORBIDDEN** from modifying, adding, rephrasing, or deleting requirements or scope text in the charter file.
2. **Ground-Truth Baseline Audit (`castle` vs `HEAD`)**:
   - The Master of Coin at Gate 3 and the Court auditor (`court audit` / `ward.py`) MUST NOT blindly evaluate against the worktree's local charter.
   - The auditor deterministically checks `git_ops.check_charter_integrity` against the original charter on `castle` (`git show castle:.court/quests/<id>.md`).
   - If ANY unauthorized changes to `# The Kingdom Requires` or `# Expected Tribute` item text are detected between `castle` and the branch, it is flagged as **Charter Tampering / Scope Laundering** and is grounds for **immediate audit failure and pillory (`court pillory`)**.
