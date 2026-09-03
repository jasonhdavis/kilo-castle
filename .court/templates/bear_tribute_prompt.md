# Bear Tribute / Report to the King — Serf Prompt Template

Send this prompt to an active Serf session when requesting a full progress report, audit, or end-of-stage Tribute handoff.

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
- **Tests Run & Results**: Exact test commands executed and their literal exit status / tail output (no paraphrasing).
- **Artifacts & Proof**: Data payloads, migrations, endpoints, or reports produced.

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
court set-section {{ quest_id }} "Tribute Rendered" --file <path_to_report>
```
(or write it directly into `.court/quests/{{ quest_id }}.md` under `# Tribute Rendered`). This ensures the Steward and Court read your rendered completion directly from disk without relying on ephemeral chat turns.
