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
- **Git Commits**: Any commits created on `{{ branch }}` with hashes and messages (or explicit statement of uncommitted working tree changes).
- **Tests Run & Results**: Exact pytest / test commands executed and their literal exit status / tail output (no paraphrasing).
- **Artifacts & Proof**: Data payloads, migrations, endpoints, or reports produced.
- **The Tally (Production & UI Verification Runbook)**:
  - **Exact URLs & UI Navigation Paths**: Specific URLs and click paths for human/QA verification (e.g. `/products/4993`, `/crm/prospecting/prospects/`, `/?tab=triggers`).
  - **Input Parameters & Filters**: Exact query parameters, dropdown filters, or form inputs to test.
  - **Example Commands & Workflows**: Exact CLI commands or scripts to run.
  - **Expected Visual & Behavioral Outcomes**: Expected UI elements, badges, states, values, or behaviors to observe to prove proper implementation.

### 3. Penance
Honest self-flagellation regarding what you failed to accomplish, half-finished, took shortcuts on, deferred, or where your confidence is shaky. Do not hide defects or paper over uncertainty with confident prose.

### 4. Audience
Specific questions or decisions that genuinely require M'Lord's judgment or authority (e.g., product trade-offs, irreversible migrations, scope changes). Do not decide these yourself. If none, state "None required."

### 5. Humble Opinion
Your concrete, technical recommendation for the immediate next steps to advance or complete this Quest.

---

### Mandatory Agent Compliance Step (Clean Tree & Base Alignment)
Before rendering your tribute and persisting your report:
1. **Working Tree Cleanliness**: Run `git status --porcelain` to verify your working tree has no uncommitted leftovers or scratch files. Stage and commit all intended deliverables.
2. **Rebase-to-Parent Alignment**: Rebase or fast-forward onto `castle` (`git rebase castle` or `git merge castle --ff-only`). Ensure your branch is cleanly aligned with `castle`'s current tip (0 commits behind `castle`) so the git tree and Agent Manager remain pristine with zero phantom diffs.

---

### Mandatory Durable Completion Requirement
In addition to your response message, you MUST persist your complete 5-section report into durable storage:
```bash
python3 .court/engine/cli.py set-section {{ quest_id }} "Tribute Rendered" --file <path_to_report>
```
(or write it directly into `.court/quests/{{ quest_id }}.md` under `# Tribute Rendered`). This ensures the Steward and Court read your rendered completion directly from disk without relying on ephemeral chat turns.

### Mandatory Self-Advance to REVIEW (do this LAST, immediately before you stop)
Persisting the Tribute file is NOT the end of this handoff. If this Quest's work is
genuinely complete (not a mid-flight progress check), re-confirm zero drift first —
re-run `git rev-list --count HEAD..castle`; if `castle` moved while you were rendering
this report, do ONE more `git merge castle` and re-check before proceeding — then run:
```bash
python3 .court/engine/cli.py advance {{ quest_id }} REVIEW --note "Tribute rendered, deferred rebase complete."
```
This is the one action that moves the Quest out of `WORKING` into `REVIEW`. A complete
Tribute section sitting under a Quest still marked `WORKING` is an incomplete handoff —
the Steward's `/levy` triage will not summon the Master of Coin on it no matter how
thorough the report is. If this report is a mid-Quest progress check rather than a
completion, skip this step and say so explicitly instead.
