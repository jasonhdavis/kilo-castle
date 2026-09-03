# The Court — Multi-Agent Orchestration & Durable State

This directory is the Steward's durable memory and the single source of truth
for all Quests and Epics — surviving across ephemeral agent sessions.

## Directory Layout
```
.court/
├── README.md              Canonical court architecture doc.
├── LEDGER.md              Steward's standing decisions and notes.
├── quests/                 Active and completed Quests (Q0NN-App-Concern.md).
├── epics/                  Multi-quest Epic initiatives.
├── archive/                Archived Quests and Epics.
└── templates/              Standard dispatch and review prompt templates.
```

## Role Hierarchy

- **M'Lord**: Human owner and final authority.
- **Steward**: Orchestrator and Observer agent. Drives the pipeline and triage.
- **Master of Coin**: Audits value delivery, acceptance criteria fulfillment, and resource costs in `REVIEW`.
- **Gatekeeper**: Runs test suites on the `gatehouse` layer and merges into `castle` in `GATE`.
- **Serf**: Disposable coding agent assigned to a single Quest worktree.
- **Vassal**: Coordinates child Quests for multi-Quest Epics.

## Pipeline Lifecycle

```
OPEN -> PLANNED -> DISPATCHED -> WORKING -> REVIEW (Master of Coin) -> GATE (Gatekeeper) -> READY_FOR_TEARDOWN -> DONE
```
(`HELD` = blocked on an Audience decision.)

## Quick CLI Reference

```bash
court status                        # Show current dashboard
court new --app <app> --concern <slug> --title "<title>" --section "<section>"
court show <id>                     # Inspect quest record
court advance <id> <STATUS>         # Advance stage
court teardown-list                 # View worktrees ready to prune
```
