# Agent Instructions & Branch Topology

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
once the test suite passes on `gatehouse`. Promoting `castle` -> `main` is the final
production release step — run `court ship` (aliases: `/cog ship`, `/ship`) beforehand to
generate the Cog Ship deployment convoy summary (Bard/Coffers/Atone/Murmur rollups plus
the `main..castle` git promotion vector) so M'Lord can review what's shipping.

---

## Division of Labor

| Stage | Runs | Scope | Notes |
|---|---|---|---|
| Scout Worktree | The Scout (spikes / POCs) | Verification that spike runs | **Never merges to gatehouse.** Generates 5-part Scout Report. |
| Serf Worktree -> `gatehouse` | Worktree Serf, pre-merge | Scoped to affected components | Cheap local checks. Before rendering Tribute: `git status --porcelain` clean + rebase/fast-forward onto `castle` (`behind: 0`). |
| Inside `gatehouse`, per merge | Gatekeeper (in `gatehouse` worktree) | Independent test suite re-verification | **NEVER run Gatekeeper as a background task, background process, or subagent on `castle`.** Processes merges strictly **one per pull / merge**, sequentially — never concurrently. |
| `gatehouse` -> `castle` (promotion) | Gatekeeper / staging session | Full suite run on `gatehouse` before promoting | Single mandatory full-suite gate |
| `castle` -> `main` (release) | `court ship` / `/cog ship` deployment convoy summary, then M'Lord | Read-only rollup + human/live QA | No redundant automated full-suite rerun on `castle`; `court ship` never merges or advances Quest status itself |

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
