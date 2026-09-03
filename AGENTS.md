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
```

`gatehouse` is the integration merge target. Changes promote from `gatehouse` -> `castle`
once the test suite passes on `gatehouse`.

---

## Division of Labor

| Stage | Runs | Scope | Notes |
|---|---|---|---|
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
| **Investigation** | Spikes, POCs, exploratory research. | Verification that spike script runs. | Never auto-merges into `gatehouse`. |
| **Ashes** | Completed / merged worktrees. | N/A | Safe for manual pruning by M'Lord. |
