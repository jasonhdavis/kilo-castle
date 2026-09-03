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
OPEN -> PLANNED -> DISPATCHED -> WORKING -> REVIEW (Master of Coin) -> GATE (Gatekeeper) -> READY_FOR_TEARDOWN -> DONE
```
(`HELD` = blocked on an Audience decision.)

## Quick CLI Reference

```bash
court status                        # Show current dashboard
court new --app <app> --concern <slug> --title "<title>" --section "<section>"
court show <id>                     # Inspect quest record
court advance <id> <STATUS>         # Advance stage
court rollup --section <type>       # Siphon tribute sections across fleet
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

## Branch Topology

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
    - scout spikes:       scout/<quest_id>-<slug> (non-merging; exploratory POCs)
```

`gatehouse` is the integration merge target. Changes promote from `gatehouse` -> `castle`
once the test suite passes on `gatehouse`.

---

## Division of Labor

| Stage | Runs | Scope | Notes |
|---|---|---|---|
| Scout Worktree | The Scout (spikes / POCs) | Verification that spike runs | **Never merges to gatehouse.** Generates 5-part Scout Report. |
| Serf Worktree -> `gatehouse` | Worktree Serf, pre-merge | Scoped to affected components | Cheap local checks |
| Inside `gatehouse`, per merge | Gatekeeper (in `gatehouse` worktree) | Independent test suite re-verification | Steward never runs tests in background |
| `gatehouse` -> `castle` (promotion) | Gatekeeper / staging session | Full suite run on `gatehouse` before promoting | Single mandatory full-suite gate |
| `castle` | Human / M'Lord | Visual QA / smoke testing | No redundant automated full-suite rerun |

---

## Agent Manager Sections

| Section / Tag | What goes here | Test scope | Promotion rule |
|---|---|---|---|
| **GATEHOUSE** | The `gatehouse` staging worktree itself. | Full suite before promoting to `castle`. | Promotes to `castle` as one reviewed step. |
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
