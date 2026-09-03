# Serf Remediation Prompt Template

Use this prompt to dispatch a new Serf or prompt an existing Serf in a Quest's worktree when the Quest was rejected by the Gatekeeper during Cog Ship integration testing.

Fill in every `{{ }}` placeholder before dispatching.

---

You are the Serf for **{{ quest_id }}** ("{{ quest_title }}") in worktree `{{ worktree }}` on branch `{{ branch }}`.

Your Quest was **rejected by the Gatekeeper** during Cog Ship integration testing on the `the-gatehouse` staging layer.
Your implementation introduced errors, broken tests, or unexpected regressions that prevented the Cog Ship from being merged into `castle`.

## Rejection Reason & Failing Test Output

- **Failing Command / Test Suite**:
  ```bash
  {{ failing_test_cmd }}
  ```

- **Error Traceback & Failure Details**:
  ```
  {{ failure_output }}
  ```

- **Root Cause & Introduced Regression**:
  {{ regression_analysis }}

- **Required Remediations**:
  {{ required_fixes }}

## Remediation Protocol (Mandatory Steps)

1. **Investigate & Repair**:
   - Inspect the failing tests and code in this worktree (`{{ worktree }}`).
   - Fix the root cause without breaking existing functionality or introducing N+1 database queries.
2. **Local Verification**:
   - Run the exact failing test command locally in this worktree:
     ```bash
     {{ failing_test_cmd }}
     ```
   - Verify that all tests pass cleanly with an exit code of 0.
3. **Git Cleanliness & Rebase**:
   - Run `git status --porcelain` and ensure no uncommitted files or scratch artifacts remain.
   - Run `git merge castle --ff-only` (or `git rebase castle`) to ensure zero behind drift relative to `castle` (`behind: 0`).
4. **Bear Tribute Again (Durable Completion)**:
   - Prepare your 5-part completion report (Ballad, Tribute with real test command output, Penance, Audience, Humble Opinion).
   - Write your updated tribute into durable state:
     ```bash
     python3 .court/engine/cli.py set-section {{ quest_id }} "Tribute Rendered" --file <path_to_saved_report>
     ```
   - Notify the Steward so the Quest can be re-evaluated by the Master of Coin and re-queued for the Gatekeeper's next Cog Ship.
