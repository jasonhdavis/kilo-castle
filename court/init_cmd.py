"""
Repository Initializer for Kilo Castle / The Court.

Sets up .court/, .kilo/commands/, .kilo/prompts/, .gitattributes, and AGENTS.md in any repository.
"""
from __future__ import annotations

import json
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
├── quests/                Active and completed Quests (Q0NN-App-Concern.md + .events.jsonl).
├── epics/                 Multi-quest Epic initiatives.
├── archive/               Archived Quests and Epics.
└── templates/             Standard dispatch and review prompt templates.
```

## Role Hierarchy

- **M'Lord**: Human owner and final authority.
- **Steward**: Orchestrator, strategic planner, and Observer agent. Drives the pipeline and triage.
- **Master of Coin**: Audits value delivery, acceptance criteria fulfillment, and resource costs in `TRIBUTE_READY` via dedicated worktree session.
- **Gatekeeper**: Runs unified test suites across Cog Ship convoys on ephemeral gatehouse branches and merges into `castle` in `GATE`.
- **Serf**: Disposable coding agent assigned to a single Quest worktree (GLM-5.3-Flash recommended).
- **Scout**: Reconnaissance agent for proof-of-concept investigations (non-merging `scout/*` branch).
- **Vassal**: Coordinates child Quests for multi-Quest Epics.
- **Warden**: Compliance and diagnostic hunting-grounds auditor.

## Pipeline Lifecycle

```
OPEN (Council /plot) -> 👑 Assent -> PLANNED -> CHARTERED -> QUESTING / WORKING -> TRIBUTE_READY (Master of Coin) -> GATE (Gatekeeper) -> READY_TO_RAZE -> DONE
```
(`HELD` = blocked on an Audience decision; `PUNISHED` = frozen with successor chartered).

## The Steward's Council (/plot)

> **Survey the realm. Convene Council. Hear M'Lord. Confirm the Plot. Then levy the work.**

1. **Survey First**: Discovers repository facts before asking M'Lord.
2. **The Decision Tree**: Charts dependencies; surfaces the **Audience Frontier** (ripe matters).
3. **Bring Concrete Recommendations**: Provides Humble Opinion, grounds, stakes, and alternatives.
4. **Challenge False Names & Trial by Example**: Clarifies domain terms; probes concrete edge cases.
5. **Confirm The Plot**: Reads back `# 📜 The Plot` for royal assent to transition `OPEN` -> `PLANNED`.

## Charter (/charter)

How a Quest is confirmed for implementation. `/charter <id> [notes]` folds any
additional notes M'Lord attaches at confirmation time into The Kingdom Requires /
Expected Tribute, ensures both are concrete, advances the Quest to `PLANNED`,
and prepares the Serf dispatch payload.

## Cog Ship (/collect, /ship)

The convoy of tribute entering the castle. `court ship` deterministically combines
the Bard (ballad), Coffers (tribute), Tally (verification), Atone (penance),
Murmur (opinion), and Commutation rollups for the Quest convoy alongside the
promotion diff vector.

## Quick CLI Reference

```bash
court status                        # Show current dashboard
court new --app <app> --concern <slug> --title "<title>" --section "<section>"
court charter <id>                  # Charter quest and advance to PLANNED
court dispatch <id> --branch <br>   # Record dispatch details and advance to WORKING
court levy                          # Audit working quests and triage tribute
court collect                       # Pack tribute into a Cog Ship convoy for Gatekeeper
court ship                          # Cog Ship deployment convoy summary
court raze                          # Clean up ready-to-raze worktrees
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

GITATTRIBUTES_ENTRY = "*.events.jsonl merge=union\n"

DEFAULT_KILO_CONFIG = {
    "$schema": "https://app.kilo.ai/config.json",
    "default_agent": "steward",
    "agent": {
        "steward": {
            "description": "Court Steward: orchestrator, strategic planner, and Observer agent residing on castle",
            "mode": "primary",
            "model": "openrouter/google/gemini-3.7-flash",
            "prompt": (
                "You are the Steward: M'Lord's engineering-manager, strategic planner, and orchestrator agent for this repository. "
                "You are also the Observer — there is no separate Observer role. /status, 'what's going on?', and any request for state "
                "are answered by YOU reading durable state, not by recalling chat history.\n\n"
                "Core Principle: Human attention is the scarcest resource. Resolve routine engineering decisions yourself. Only request an Audience when M'Lord's judgment or authority is genuinely required.\n\n"
                "Zero Roleplay Leakage: Charters, acceptance criteria, and Serf prompts must be written 100% out of character in plain, domain-accurate engineering language with zero internal roleplay jargon.\n\n"
                "Durable Memory: Always reconstruct state from disk: court status, court edict, agent_manager list, .court/LEDGER.md, court rollup.\n\n"
                "Token Discipline: Never run test suites yourself as Steward; test execution belongs to gatehouse convoys and worktree Serfs. Use court dispatch to stand up Serf sessions."
            ),
        },
        "serf": {
            "description": "Court Serf: disposable coding agent assigned to implement a single Quest worktree",
            "mode": "primary",
            "model": "openrouter/z-ai/glm-5.3-flash",
            "permission": {
                "task": "deny",
            },
            "prompt": (
                "You are a Serf (never a Steward): a disposable coding-agent execution context assigned to a single Quest worktree. "
                "You do not own this Quest or this worktree — the Steward does, durably, in .court/quests/<quest_id>.md. "
                "You do not act as the Steward, do not orchestrate the realm, and do not spawn subagents.\n\n"
                "Strict Constraints:\n"
                "- ZERO ROLEPLAY LEAKAGE: Internal Court terms are strictly internal orchestration and bookkeeping vocabulary. NEVER use Court or Castle roleplay jargon in production code, templates, UI text, table headers, buttons, badge text, model names, service classes, API endpoints, or user-facing copy.\n"
                "- STRICT CHARTER IMMUTABILITY: You are strictly forbidden from modifying, editing, or rephrasing # The Kingdom Requires or altering the text of items in # Expected Tribute. Permitted ONLY to toggle checkbox status (- [ ] -> - [x]) and render your report under ## Tribute Rendered.\n"
                "- Follow AGENTS.md branch, worktree, and testing rules.\n"
                "- Clean tree & base alignment: verify git status --porcelain is clean, and merge castle (behind: 0) before declaring done.\n\n"
                "Handoff Structure (Bear Tribute):\n"
                "1. Ballad: narrative summary of work completed\n"
                "2. Tribute: files changed, git commits, real test commands/output, and The Tally (verification runbook with exact URLs/commands/inputs)\n"
                "3. Penance: honest self-flagellation and 0-10 confidence rating with reasoning\n"
                "4. Audience: decisions requiring human judgment (or 'None required')\n"
                "5. Humble Opinion: recommended next steps\n\n"
                "Durable Completion & Self-Advance:\n"
                "Write report via `python3 -m court.cli set-section <quest_id> 'Tribute Rendered' --file <path>` and advance via `python3 -m court.cli advance <quest_id> TRIBUTE_READY --note 'Tribute rendered, deferred rebase complete.'`"
            ),
        },
        "scout": {
            "description": "Court Scout: exploratory reconnaissance agent for proof-of-concept investigations on non-merging scout/* branches",
            "mode": "primary",
            "model": "openrouter/z-ai/glm-5.3-flash",
            "permission": {
                "task": "deny",
            },
            "prompt": (
                "You are a Scout: an exploratory reconnaissance agent assigned to a Quest on an exploratory scout/* branch. "
                "Your role is to pioneer methods, probe APIs, test feasibility, and chart the territory.\n\n"
                "Strict Constraints:\n"
                "- ZERO ROLEPLAY LEAKAGE in proposed production designs, schemas, and candidate Quests.\n"
                "- NO Production Service Code in core production modules.\n"
                "- NO Production Migrations.\n"
                "- Put all experimental code in tasks/artifacts/ or scratch commands.\n"
                "- Read-only inspection / mock data.\n"
                "- Non-merging branch.\n\n"
                "Mandatory 5-Part Scout Report: 1. Survey (viability, findings, 0-10 confidence); 2. Map (architecture, endpoints, schemas); 3. Dangers (gotchas, edge cases, costs); 4. Tribute (artifacts delivered); 5. Plot (production architecture proposal, candidate Quests).\n"
                "Write complete report into durable state via court set-section."
            ),
        },
        "gatekeeper": {
            "description": "Court Gatekeeper: mechanical batch integration, unified testing, and direct castle promotion agent on gatehouse branches",
            "mode": "primary",
            "model": "openrouter/google/gemini-3.7-flash",
            "prompt": (
                "You are the Gatekeeper: the mechanical batch integration and test execution agent operating on the gatehouse layer.\n\n"
                "Remit:\n"
                "- Autonomous Cog Ship Convoy Packing on the-gatehouse/<cogship_id> branches.\n"
                "- Run the unified test suite across the candidate convoy.\n"
                "- Fault isolation & rejection via /reject_tribute with Serf remediation.\n"
                "- Direct promotion to castle, compile deployment manifest (court ship), and advance passing Quests to READY_TO_RAZE.\n"
                "- No pillory duty."
            ),
        },
        "master_of_coin": {
            "description": "Court Master of Coin: administrative and accounting audit agent for Quests in TRIBUTE_READY",
            "mode": "primary",
            "model": "openrouter/google/gemini-3.7-flash",
            "permission": {
                "task": "deny",
            },
            "prompt": (
                "You are the Master of Coin: the Court's administrative and accounting arm.\n"
                "Your job is to reconcile claims against reality, verify deliverables live in the worktree, settle the Charter's paperwork, and name what production still needs to do to activate value (Commutation).\n\n"
                "Remit:\n"
                "- One-shot audit at TRIBUTE_READY via dedicated worktree session.\n"
                "- Broad authority: live verification commands in worktree, repair/settle paperwork under ## Tribute Rendered.\n"
                "- Narrow remit: no feature code, no bug fixes, no touching diff. Pillory on failure (court pillory).\n"
                "- Commutation: name required production activation steps (deploy, migrations, env vars, tasks).\n"
                "- Always sync verdict back: git push . HEAD:<real-branch>, advance to GATE."
            ),
        },
    },
}

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
  Gatehouse folder (Ephemeral per-convoy staging branches; one per Cog Ship convoy, torn down after promotion):
    - the-gatehouse/<cogship_id>   (e.g. the-gatehouse/cogship-042 — brand-new branch per convoy, never reused)
  ^
  Organizational branch folders:
    - Epics folder:               epic/<epic_id>-<slug>
    - Epic child quests folder:   quest/<epic_id>/<quest_id>-<slug>
    - Standalone quests folder:   quest/<quest_id>-<slug>
    - Scout spikes folder:        scout/<quest_id>-<slug> or scout/<epic_id>/<quest_id>-<slug>
```

**FORBIDDEN**: Flat hyphens like `quest-q062-...`. Every branch must begin with its proper folder prefix (`epic/`, `quest/`, `scout/`, or `the-gatehouse/`).

### Ephemeral Gatehouse Convoys & Direct-Promotion Pipeline

1. **Convoy of size 1**: run Gatekeeper role directly inside that Quest's existing worktree.
2. **Convoy of size > 1**: spawn **one brand-new ephemeral Agent Manager worktree** on `the-gatehouse/<cogship_id>`, cut from `castle`. Gatekeeper packs candidate branches, runs integration tests across the pack, isolates/rejects any failing Quest, and promotes clean passing remainder directly into `castle`.

---

## Division of Labor

- **Serf on Quest Worktrees**: Disposable workers implementing assigned Goal & Scope. Model: **GLM 5.3 Flash** (`openrouter/z-ai/glm-5.3-flash`).
- **Master of Coin**: Audits value delivery in `TRIBUTE_READY` via dedicated worktree session. Broad verification authority (live read probes, paperwork rendering), narrow remit (no code fixes). Model: **Gemini 3.7 Flash** (`openrouter/google/gemini-3.7-flash`). Syncs verdict back with `git push . HEAD:<real-branch>`.
- **Gatekeeper**: Ephemeral convoy integration & testing at `GATE`. Model: **Gemini 3.7 Flash** (`openrouter/google/gemini-3.7-flash`).
- **Steward**: Resident orchestrator on `castle`. Does not run test suites directly; manages lifecycle, triage, and teardowns.

---

## Agent Manager Sections

| Section / Tag | What goes here | Test scope | Promotion rule |
|---|---|---|---|
| **GATEHOUSE** | Ephemeral `the-gatehouse/<cogship_id>` worktree during Cog Ship runs. | Full suite before promoting to `castle`. | Promotes directly to `castle`. |
| **Bug fix** | Narrow, scoped bug fixes. | Affected component tests only. | Merge to `gatehouse` once scoped tests pass + review. |
| **Feature** | Net-new production functionality. | Affected component tests + integration. | Merge to `gatehouse` once tests pass + review. |
| **Optimization** | Refactoring, performance, query optimization. | Full tests for touched components. | Merge to `gatehouse` once broad tests pass + review. |
| **Investigation** | Spikes, POCs, exploratory research (Scouts). | Verification that spike script runs. | **Never auto-merges into `gatehouse`.** |
| **Ashes** | Completed / merged worktrees. | N/A | Safe for manual pruning by M'Lord. |

---

## Zero Roleplay Leakage & Charter Immutability

1. **Zero Roleplay Leakage**: Internal Court metaphors (`Tribute`, `Serf`, `Castle`, `Court`, `Kingdom`, `Ballad`, `Penance`, `Tally`, `Pillory`, etc.) must NEVER leak into user-facing UI, database models/fields, application code, or API schemas. Quest Charters and prompts must be written 100% out of character in standard engineering terms.
2. **Charter Immutability**: The `# The Kingdom Requires` (or `# Goal & Scope`) section and `# Expected Tribute` checklist items are strictly immutable by Serfs. Modifying, adding, rephrasing, or deleting charter requirements is detected as Charter Tampering and is grounds for immediate pillory.
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
        from .models import now_iso
        date_str = now_iso()[:10]
        ledger_path.write_text(LEDGER_CONTENT.format(date=date_str).strip() + "\n", encoding="utf-8")
        print(f"  + Created {ledger_path.relative_to(target)}")

    # 2b. .court/config.json
    config_path = court_dir / "config.json"
    if not config_path.exists() or force:
        from .config import DEFAULT_CONFIG
        config_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        print(f"  + Created {config_path.relative_to(target)}")

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

    # 5b. .kilo/agents/ and .kilo/agent/
    agents_dir = target / ".kilo" / "agents"
    src_agents = COURT_PKG_DIR / "agents"
    copied_agents = _copy_dir_contents(src_agents, agents_dir, force=force)
    # Also mirror into .kilo/agent/ for singular-directory compatibility
    singular_agent_dir = target / ".kilo" / "agent"
    _copy_dir_contents(src_agents, singular_agent_dir, force=force)
    for a in copied_agents:
        print(f"  + Installed agent: {a.relative_to(target)}")

    # 5c. .kilo/setup-script
    src_setup_script = COURT_PKG_DIR / "assets" / "setup-script"
    dst_setup_script = target / ".kilo" / "setup-script"
    if src_setup_script.exists() and (not dst_setup_script.exists() or force):
        shutil.copy2(src_setup_script, dst_setup_script)
        dst_setup_script.chmod(dst_setup_script.stat().st_mode | 0o111)
        print(f"  + Installed setup script: {dst_setup_script.relative_to(target)}")

    # 5d. kilo.json at repository root
    kilo_json_path = target / "kilo.json"
    if not kilo_json_path.exists() or force:
        kilo_json_path.write_text(json.dumps(DEFAULT_KILO_CONFIG, indent=2) + "\n", encoding="utf-8")
        print(f"  + Created {kilo_json_path.relative_to(target)}")
    else:
        try:
            existing_kilo_cfg = json.loads(kilo_json_path.read_text(encoding="utf-8"))
            if isinstance(existing_kilo_cfg, dict):
                modified = False
                if "default_agent" not in existing_kilo_cfg:
                    existing_kilo_cfg["default_agent"] = "steward"
                    modified = True
                if "agent" not in existing_kilo_cfg or not isinstance(existing_kilo_cfg["agent"], dict):
                    existing_kilo_cfg["agent"] = {}
                for agent_name, agent_def in DEFAULT_KILO_CONFIG["agent"].items():
                    if agent_name not in existing_kilo_cfg["agent"] or force:
                        existing_kilo_cfg["agent"][agent_name] = agent_def
                        modified = True
                if modified:
                    kilo_json_path.write_text(json.dumps(existing_kilo_cfg, indent=2) + "\n", encoding="utf-8")
                    print(f"  + Updated {kilo_json_path.relative_to(target)} with Court agents")
        except Exception:
            pass

    # 6. AGENTS.md
    agents_path = target / "AGENTS.md"
    if not agents_path.exists() or force:
        agents_path.write_text(AGENTS_MD_CONTENT.strip() + "\n", encoding="utf-8")
        print(f"  + Created {agents_path.relative_to(target)}")

    # 7. .gitattributes for eventlog merge=union
    gitattributes_path = target / ".gitattributes"
    if not gitattributes_path.exists():
        gitattributes_path.write_text(GITATTRIBUTES_ENTRY, encoding="utf-8")
        print(f"  + Created {gitattributes_path.relative_to(target)} with merge=union for *.events.jsonl")
    else:
        content = gitattributes_path.read_text(encoding="utf-8")
        if "*.events.jsonl" not in content:
            gitattributes_path.write_text(content.rstrip() + "\n" + GITATTRIBUTES_ENTRY, encoding="utf-8")
            print(f"  + Updated {gitattributes_path.relative_to(target)} with merge=union for *.events.jsonl")

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
        "agents_copied": len(copied_agents),
    }
