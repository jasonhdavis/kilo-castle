"""
Repository Initializer for Kilo Castle / The Court.

Sets up .court/, .kilo/commands/, .kilo/prompts/, and AGENTS.md in any repository.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

COURT_PKG_DIR = Path(__file__).resolve().parent

COURT_README_CONTENT = """# The Court — Multi-Agent Orchestration & Durable State

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
court tally                         # Siphon production verification runbooks across fleet
court ship                          # Cog Ship: deployment convoy summary (Bard/Coffers/Atone/Murmur + castle..main vector)
court teardown-list                 # View worktrees ready to prune
```
"""

LEDGER_CONTENT = """# The Court Ledger

Durable running log of Steward decisions, cross-Quest coordination, and Audience records.

## Standing Architectural Decisions

| Date | Topic | Decision | Notes |
|---|---|---|---|
| {date} | Initialization | Court initialized with Kilo Castle | Standalone multi-agent orchestration layer active |

## Audience Log Index

| Date | Quest | Decision Required | Verdict / Outcome |
|---|---|---|---|
"""

AGENTS_MD_CONTENT = """# Agent Instructions & Branch Topology

This repository uses **Kilo Castle** for deterministic multi-agent orchestration with Git Worktrees.

---

## Branch Topology & Git Organizational Folders

All branches MUST use forward-slash (`/`) folder hierarchy without exception:

```
main                    (production root trunk)
  ^
castle                  (staging root trunk for main; attached to localhost)
  ^
  Gatehouse Stations folder (Autonomous Direct-Promotion Stations; Dynamic Rolling: North -> South -> East -> West):
    - the-gatehouse/north     (Autonomous Cog Ship staging station)
    - the-gatehouse/south     (Autonomous Cog Ship staging station)
    - the-gatehouse/east      (Autonomous Cog Ship staging station)
    - the-gatehouse/west      (Autonomous Cog Ship staging station)
  ^
  Organizational branch folders:
    - Epics folder:               epic/<epic_id>-<slug>
    - Epic child quests folder:   quest/<epic_id>/<quest_id>-<slug>
    - Standalone quests folder:   quest/<quest_id>-<slug>
    - Scout spikes folder:        scout/<quest_id>-<slug> or scout/<epic_id>/<quest_id>-<slug>
```

**FORBIDDEN**: Flat hyphens like `quest-q062-...` or `the-gatehouse-2`. Every branch must begin with its proper folder prefix (`epic/`, `quest/`, `scout/`, or `the-gatehouse/`).

### The Four Autonomous Gatehouse Stations & Direct-Promotion Pipeline

To maximize throughput and prevent bottlenecks or double-gating, the staging layer is organized into **Four Autonomous Gatehouse Stations** nested under the `the-gatehouse/` folder:

**Dynamic Availability Rolling (No Domain Silos & No Central Bottleneck)**:
Gatehouses are **not** restricted by domain. **Any gatehouse station can pack, test, approve, and promote any Cog Ship directly into `castle`.** There is no intermediate `central` gatehouse — having a central bottleneck would create serial merge contention and double-gating. When candidate Quests at `GATE` are ready for batch integration, the Court simply rolls down the list to whichever regional station is currently idle/available: **North → South → East → West → North...**

1. **`the-gatehouse/north`** (`.kilo/worktrees/the-gatehouse-north`): North Station — autonomous Cog Ship staging, testing, fault isolation, and direct promotion to `castle`.
2. **`the-gatehouse/south`** (`.kilo/worktrees/the-gatehouse-south`): South Station — autonomous Cog Ship staging, testing, fault isolation, and direct promotion to `castle`.
3. **`the-gatehouse/east`** (`.kilo/worktrees/the-gatehouse-east`): East Station — autonomous Cog Ship staging, testing, fault isolation, and direct promotion to `castle`.
4. **`the-gatehouse/west`** (`.kilo/worktrees/the-gatehouse-west`): West Station — autonomous Cog Ship staging, testing, fault isolation, and direct promotion to `castle`.

---

## Division of Labor

**Gatekeeper Execution Environment, Cog Ship Mandate & Remediation Protocol:**
- **Dedicated Agent Manager Session on `the-gatehouse/<station>`**: The Gatekeeper MUST ALWAYS run as an Agent Manager session inside a persistent Gatehouse station worktree (`.kilo/worktrees/the-gatehouse-north`, `the-gatehouse-south`, `the-gatehouse-east`, `the-gatehouse-west`). **NEVER run Gatekeeper as a background task, background process, or subagent on `castle`.**
- **Model Tiering**: Deliberately a **Claude Sonnet Latest** class agent (`openrouter/anthropic/claude-sonnet-latest`), standing as the smartest checkpoint in the pipeline.
- **Sequential Non-Background Tasks Permitted**: The Gatekeeper inside a Gatehouse station is explicitly allowed to spawn **sequential non-background subagent tasks** (`task` tool with `background: false`) for integration checks, diff inspection, or test verification. Background tasks are forbidden.
- **Cog Ship Packing & Single Unified Merge/Test**: Gatekeeper does NOT perform redundant line-by-line manual code re-audits on individual Quests (Master of Coin already approved scope and value in `REVIEW`). Gatekeeper surveys Quests waiting at `GATE`, decides the **Cog Ship convoy batch** to pack, merges candidate branches into the assigned station branch (`the-gatehouse/<station>`), and executes the unified integration test suite across the pack all at once.
- **Fault Isolation, Commit Rejection & Re-testing**: If tests fail during the unified run, Gatekeeper isolates/re-tests which specific commit or Quest caused the failure, **rejects the offending commit/Quest** from the current Cog Ship, and rolls back its merge. The clean passing pack continues forward.
- **Serf Remediation Dispatch**: For any rejected Quest, Gatekeeper writes the exact failure traceback into `# Gatekeeper Review`, returns the Quest to `WORKING`, and **dispatches/prompts a Serf session in the Quest's worktree** with the exact error details and remediation instructions.
- **Direct Promotion to Castle**: Passing Cog Ships are promoted **directly into `castle`**, compiled into the deployment manifest (`court ship`), and advanced to `READY_FOR_TEARDOWN`.

| Stage | Runs | Scope | Notes |
|---|---|---|---|
| Scout Worktree | The Scout (spikes / POCs) | Verification that spike runs | **Never merges to gatehouse.** Generates 5-part Scout Report. |
| Serf Worktree -> `gatehouse` | Worktree Serf, pre-merge | Scoped to affected components | Cheap local checks. Before rendering Tribute: `git status --porcelain` clean + rebase/fast-forward onto `castle` (`behind: 0`). |
| Inside `gatehouse`, per convoy | Gatekeeper (in station worktree) | Unified batch integration & test execution | Single merge & test across Cog Ship convoy. Fault isolation & Serf remediation on failure. |
| `gatehouse` -> `castle` (promotion) | Gatekeeper / staging session | Direct promotion into `castle` | Promotes clean verified Cog Ships directly into castle. |
| `castle` -> `main` (release) | `court ship` / `/cog ship` deployment convoy summary, then M'Lord | Read-only rollup + human/live QA | No redundant automated full-suite rerun on `castle`. |

---

## Agent Manager Sections

| Section / Tag | What goes here | Test scope | Promotion rule |
|---|---|---|---|
| **GATEHOUSE** | The `gatehouse` staging worktrees (`the-gatehouse/north`, `south`, `east`, `west`). | Full suite before promoting to `castle`. | Promotes directly to `castle` as one reviewed step. |
| **Bug fix** | Narrow, scoped bug fixes. | Affected component tests only. | Merge to `gatehouse` once scoped tests pass + review. |
| **Feature** | Net-new production functionality. | Affected component tests + integration. | Merge to `gatehouse` once tests pass + review. |
| **Optimization** | Refactoring, performance, query optimization. | Full tests for touched components. | Merge to `gatehouse` once broad tests pass + review. |
| **Investigation** | Spikes, POCs, exploratory research (Scouts). | Verification that spike script runs. | **Never auto-merges into `gatehouse`.** Produces Scout Report for M'Lord to blueprint production Quests. |
| **Ashes** | Completed / merged worktrees. | N/A | Safe for manual pruning by M'Lord. |
"""


def _copy_dir_contents(src_dir: Path, dst_dir: Path, force: bool = False) -> list[Path]:
    dst_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    if not src_dir.exists():
        return copied
    for src_file in src_dir.glob("*.md"):
        dst_file = dst_dir / src_file.name
        if not dst_file.exists() or force:
            shutil.copy2(src_file, dst_file)
            copied.append(dst_file)
    return copied


def run_init(target_dir: Optional[Path] = None, force: bool = False) -> dict:
    target = (target_dir or Path.cwd()).resolve()
    print(f"Initializing Kilo Castle in {target}...")

    court_dir = target / ".court"
    quests_dir = court_dir / "quests"
    epics_dir = court_dir / "epics"
    archive_dir = court_dir / "archive"
    templates_dir = court_dir / "templates"

    for d in (quests_dir, epics_dir, archive_dir, templates_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 1. .court/README.md
    readme_path = court_dir / "README.md"
    if not readme_path.exists() or force:
        readme_path.write_text(COURT_README_CONTENT.strip() + "\n", encoding="utf-8")
        print(f"  + Created {readme_path.relative_to(target)}")

    # 2. .court/LEDGER.md
    ledger_path = court_dir / "LEDGER.md"
    if not ledger_path.exists() or force:
        from court.models import now_iso
        date_str = now_iso()[:10]
        ledger_path.write_text(LEDGER_CONTENT.format(date=date_str).strip() + "\n", encoding="utf-8")
        print(f"  + Created {ledger_path.relative_to(target)}")

    # 3. .court/templates/
    src_templates = COURT_PKG_DIR / "templates"
    copied_templates = _copy_dir_contents(src_templates, templates_dir, force=force)
    for t in copied_templates:
        print(f"  + Copied template: {t.relative_to(target)}")

    # 4. .kilo/commands/
    commands_dir = target / ".kilo" / "commands"
    src_commands = COURT_PKG_DIR / "commands"
    copied_commands = _copy_dir_contents(src_commands, commands_dir, force=force)
    for c in copied_commands:
        print(f"  + Installed command: {c.relative_to(target)}")

    # 5. .kilo/prompts/
    prompts_dir = target / ".kilo" / "prompts"
    src_prompts = COURT_PKG_DIR / "prompts"
    copied_prompts = _copy_dir_contents(src_prompts, prompts_dir, force=force)
    for p in copied_prompts:
        print(f"  + Installed prompt: {p.relative_to(target)}")

    # 6. AGENTS.md
    agents_path = target / "AGENTS.md"
    if not agents_path.exists() or force:
        agents_path.write_text(AGENTS_MD_CONTENT.strip() + "\n", encoding="utf-8")
        print(f"  + Created {agents_path.relative_to(target)}")

    print("\nKilo Castle initialization complete!")
    print("Next steps:")
    print("  1. Create a Quest: court new --app core --concern my-feature --title 'My Feature' --section 'Feature'")
    print("  2. Launch a Scout: /scout core prototype-auth 'Test OAuth2 feasibility'")
    print("  3. Check status:   court status")
    print("  4. Use slash commands inside Kilo: /charter, /levy, /collect, /status, /plot, /scout")

    return {
        "court_dir": court_dir,
        "templates_copied": len(copied_templates),
        "commands_copied": len(copied_commands),
        "prompts_copied": len(copied_prompts),
    }
