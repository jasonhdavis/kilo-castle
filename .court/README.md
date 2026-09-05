# The Court — Multi-Agent Orchestration & Durable State

This directory is the Steward's durable memory and the single source of truth
for all Quests and Epics — surviving across ephemeral agent sessions.

## Directory Layout
```
.court/
├── README.md              Canonical court architecture doc.
├── LEDGER.md              Steward's standing decisions and notes.
├── EDICTS.md              Royal decrees and strategic priorities.
├── quests/                 Active and completed Quests (Q0NN-App-Concern.md).
├── epics/                  Multi-quest Epic initiatives.
├── archive/                Archived Quests and Epics.
├── templates/              Standard dispatch and review prompt templates.
└── ward/                   Warden patrol reports and the Wardens' patrol log.
```

## Role Hierarchy

- **M'Lord**: Human owner and final authority.
- **Steward**: Orchestrator, strategic planner, and Observer agent. Drives the pipeline and triage.
- **Master of Coin**: Audits value delivery, acceptance criteria fulfillment, and resource costs in `REVIEW`.
- **Gatekeeper**: Runs test suites on the `gatehouse` layer and merges into `castle` in `GATE`.
- **Serf**: Disposable coding agent assigned to a single Quest worktree.
- **Scout**: Reconnaissance agent for proof-of-concept investigations (non-merging `scout/*` branch).
- **Vassal**: Coordinates child Quests for multi-Quest Epics.
- **Warden**: Dedicated log-patrol and error-hunting agent on non-merging `ward/*` branches; files Warden Reports for the Steward to charter into Quests.

## Pipeline Lifecycle

```
OPEN (Council /plot) -> 👑 Assent -> PLANNED -> DISPATCHED -> WORKING -> REVIEW (Master of Coin) -> GATE (Gatekeeper) -> READY_FOR_TEARDOWN -> DONE
```
(`HELD` = blocked on an Audience decision. `PUNISHED` = a side-state reached only via the
pillory, from any stage — see "Side-States: PUNISHED & the Pillory" below.)

## The Steward's Council (/plot)

> **Survey the realm. Convene Council. Hear M'Lord. Confirm the Plot. Then levy the work.**

1. **Survey First**: Discovers repository facts before asking M'Lord.
2. **The Decision Tree**: Charts dependencies; surfaces the **Audience Frontier** (ripe matters).
3. **Bring Concrete Recommendations**: Provides Humble Opinion, grounds, stakes, and alternatives.
4. **Challenge False Names & Trial by Example**: Clarifies domain terms; probes concrete edge cases.
5. **Confirm The Plot**: Reads back `# 📜 The Plot` for royal assent to transition `OPEN` -> `PLANNED`.

## Charter

How a Quest is confirmed for implementation. `/charter <id> [notes]` folds any
additional notes M'Lord attaches at confirmation time into the Quest's Goal & Scope /
Expected Tribute, ensures both are concrete, and advances the Quest to `PLANNED`.
Charter is the green light: once chartered, the Steward proceeds straight to
`/dispatch` on its own judgment — no further Audience round required for that Quest
unless something genuinely new and material surfaces mid-implementation. It doubles as
the fast lane for well-understood asks (skip the full `/plot` Council when the intent
is already clear) and as the closing act that seals a Council's Plot into a confirmed,
dispatch-ready Quest.

## Cog Ship

The convoy of tribute entering the castle. `court ship` (aliases: `/cog ship`, `/ship`)
deterministically combines the Bard (ballad), Coffers (tribute), Atone (penance), and
Murmur (opinion) rollups for the Quest convoy (default filter: `READY_FOR_TEARDOWN` +
`DONE`), alongside the raw `castle..main` git promotion vector (ahead/behind, commit log,
diffstat). It is read-only reporting — run it as the closing step of `/collect` and again
on demand before an actual `castle` -> `main` promotion decision.

## The Ward & The Warden Archetype

`ward.py` is the Court's compliance-auditor subsystem: a deterministic subsystem, not an
LLM call, that computes realm-wide health in one pass. `court ward` reports:
- The last log-patrol survey timestamp (when a Warden last ran).
- Pending Warden Reports awaiting the Steward to charter them into Quests.
- Realm compliance health: Quests with stale base drift, dirty worktrees, or incomplete
  Tribute across the whole fleet.
- With `--check-fresh`: an optional, project-supplied error-ingestion harness is invoked
  to surface fresh unresolved issues (see `court ward --check-fresh`). This step is
  entirely optional and project-specific — a project with no such harness simply omits
  it and the Warden works from manually-reported bugs instead.

The **Warden** is a dedicated persona, distinct from both the Steward and a Serf: its sole
duty is to patrol logs and error trackers on a non-merging `ward/*` branch and file a
5-part **Warden Report** for every fresh, escalation-worthy anomaly it finds:
1. 🧭 **Survey** — what was found, how often, since when, and a triage verdict
   (Escalate Now / Monitor / Noise).
2. 🩻 **Stack Trace** — the raw traceback or log excerpt, with file:line references
   where determinable.
3. 💥 **Impact** — affected users/accounts, volume, severity tier.
4. 🔬 **Root Cause Diagnosis** — the underlying cause, not just the symptom.
5. 🔧 **Proposed Fix** — a concrete, scoped remediation the Steward can charter
   directly as a `Bug fix` Quest.

The Warden never merges `ward/*` branches and never writes remediation code — diagnosis
only. The Steward reviews filed reports via `/ward` and charters production Quests from
them directly.

## Side-States: PUNISHED & the Pillory

Most Quests never leave the main pipeline. `PUNISHED` is a side-state reachable from any
stage, used only for a genuine judgment-call rejection (wrong value, wrong scope, or a
duplicate of something that already exists):

```
any stage --[pillory]--> PUNISHED --[successor quest chartered]--> frozen worktree --[successor completes]--> razed together with successor
```

- Pillorying a Quest sets its status to `PUNISHED` and writes a `## Judgement of the
  Condemned` section with the Reason and the Decrees Issued for a successor.
- `pilloried_by` (on the punished Quest) and `pillory_of` (on its successor) link the
  pair once the successor is chartered.
- The punished Quest's worktree is frozen — read-only, never re-entered by a Serf, kept
  visible rather than silently deleted.
- **There is no return to WORKING from a pillory judgment.** A punished Quest is not
  fixed in place; a fresh Quest and worktree are chartered from the Decrees instead.
- At raze time, a `PUNISHED` Quest is never torn down alone — it is razed together with
  its successor once the successor reaches `READY_FOR_TEARDOWN`/`DONE`.

### The mechanical-rejection carve-out

Not every rejection is a pillory. Routine integration/merge-test failures — a failing
test, a merge regression — stay in the rejecting role's own lane and return the Quest to
`WORKING` for a same-worktree fix (see `.kilo/commands/reject_tribute.md`). The pillory
is reserved for the review/audit role's value/scope/duplication judgment calls, not for
anything a fresh Serf can mechanically patch in the same worktree.

## Model-Tiering Principle

Different roles carry different cost/judgment trade-offs, and the Court treats the actual
model choice per role as a **configurable convention, not hardcoded law**: fast, cheap
models are appropriate for high-volume mechanical execution work (Serfs churning through
scoped implementation, Scouts running throwaway spikes), while a stronger model is
appropriate for judgment-heavy review (auditing value, scope, and duplication before
expensive test/merge cycles run). Whichever model is *actually* used for a given Quest
should be recorded in that Quest's own frontmatter fields (e.g. `serf_model`,
`master_of_coin_model`, `gatekeeper_model`) rather than assumed from generic defaults —
the frontmatter is the source of truth for what ran, not whatever the current default
happens to be.

## Castle Worktree Hygiene & Immediate Commits

Commit `.court/` bookkeeping changes (new Quests, ledger updates, status advances) to the
base/staging branch (`castle`) promptly rather than letting them sit uncommitted. New
worktrees are created from `castle`'s current tip, so a `castle` that is behind on its own
bookkeeping means every new worktree starts from a stale baseline.

## Token / Context Discipline

- Deterministic CLI checks (e.g. `court verify`, `court rebase --dry-run`, `court ward`)
  record pass/fail state without burning any LLM tokens — prefer them over an agent
  turn wherever the check is purely mechanical.
- Orchestrating roles should default to re-reading the Court's quest files fresh each
  session rather than trusting potentially-stale chat memory; chat context can be lost,
  compacted, or reset at any time, but the Markdown on disk cannot.

## Quick CLI Reference

```bash
court status                        # Show current dashboard
court new --app <app> --concern <slug> --title "<title>" --section "<section>"
court show <id>                     # Inspect quest record
court advance <id> <STATUS>         # Advance stage
court rollup --section <type>       # Siphon tribute sections across fleet
court ship                          # Cog Ship: deployment convoy summary (Bard/Coffers/Atone/Murmur + castle..main vector)
court ward                          # Compliance/patrol dashboard: last survey, pending Warden Reports, realm health
court teardown-list                 # View worktrees ready to prune
```
