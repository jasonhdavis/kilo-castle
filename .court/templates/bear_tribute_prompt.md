# Bear Tribute / Report to the King — Serf Prompt Template

Send this prompt to an active Serf session (`agent_manager` `prompt`) when requesting a full progress report, audit, or end-of-stage Tribute handoff.

---

Serf of **{{ quest_id }}** ("{{ quest_title }}"):

M'Lord demands you **Bear Tribute** and submit your formal **Report to the King**.
Provide your comprehensive report structured strictly into these five sections:

### 1. Ballad
A narrative summary of work completed, problem context uncovered, architectural paths chosen, and technical decisions made during this session.

### 2. Tribute
Provable, tangible work product delivered. You must include:
- **Files Touched / Created**: Exact file paths with line counts or diff stat.
- **Git Commits**: Any commits created on `{{ branch }}` with hashes and messages.
- **Tests Run & Results**: Exact test commands executed and their literal exit status / tail output.
- **Artifacts & Proof**: Data payloads, migrations, endpoints, or reports produced.
- **The Tally (Production & UI Verification Runbook)**:
  - Exact URLs & navigation paths for verification.
  - Input parameters, filters, or form inputs to test.
  - Example commands and workflows.
  - Expected visual & behavioral outcomes.

### 3. Penance
Honest self-flagellation regarding what you failed to accomplish, half-finished, took shortcuts on, deferred, or where your confidence is shaky. **Give a confidence-of-completion rating, 0-10, with your reasoning.**

### 4. Audience
Specific questions or decisions that genuinely require M'Lord's judgment or authority. If none, state "None required."

### 5. Humble Opinion
Your concrete technical recommendation for the immediate next steps to advance or complete this Quest.

---

### Mandatory Agent Compliance Step (Clean Tree & Base Alignment)
Before rendering your tribute and persisting your report:
1. **Working Tree Cleanliness**: Run `git status --porcelain` to verify your working tree has no uncommitted leftovers or scratch files.
2. **Rebase-to-Parent Alignment**: Rebase or fast-forward onto `castle` (`git rebase castle` or `git merge castle --ff-only`). Ensure your branch is cleanly aligned with `castle`'s current tip (0 commits behind `castle`).

---

### Mandatory Durable Completion Requirement
Persist your complete 5-section report into durable storage:
```bash
python3 -m court.cli set-section {{ quest_id }} "Tribute Rendered" --file <path_to_report>
```

### Mandatory Self-Advance to TRIBUTE_READY (do this LAST, immediately before you stop)
Re-confirm zero drift, then run:
```bash
python3 -m court.cli advance {{ quest_id }} TRIBUTE_READY --note "Tribute rendered, deferred rebase complete. Tribute Ready for Master of Coin."
```
