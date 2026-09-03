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
└── templates/              Standard dispatch and review prompt templates.
```

## Role Hierarchy

- **M'Lord**: Human owner and final authority.
- **Steward**: Orchestrator, strategic planner, and Observer agent. Drives the pipeline and triage.
- **Master of Coin**: Audits value delivery, acceptance criteria fulfillment, and resource costs in `REVIEW`.
- **Gatekeeper**: Runs test suites on the `gatehouse` layer and merges into `castle` in `GATE`.
- **Serf**: Disposable coding agent assigned to a single Quest worktree.
- **Scout**: Reconnaissance agent for proof-of-concept investigations (non-merging `scout/*` branch).
- **Vassal**: Coordinates child Quests for multi-Quest Epics.

## Pipeline Lifecycle

```
OPEN (Council /plot) -> 👑 Assent -> PLANNED -> DISPATCHED -> WORKING -> REVIEW (Master of Coin) -> GATE (Gatekeeper) -> READY_FOR_TEARDOWN -> DONE
```
(`HELD` = blocked on an Audience decision.)

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

## Quick CLI Reference

```bash
court status                        # Show current dashboard
court new --app <app> --concern <slug> --title "<title>" --section "<section>"
court show <id>                     # Inspect quest record
court advance <id> <STATUS>         # Advance stage
court rollup --section <type>       # Siphon tribute sections across fleet
court ship                          # Cog Ship: deployment convoy summary (Bard/Coffers/Atone/Murmur + castle..main vector)
court teardown-list                 # View worktrees ready to prune
```
