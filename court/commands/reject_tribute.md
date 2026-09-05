---
description: Reject a Quest's tribute at GATE for same-worktree Serf remediation — the Gatekeeper's mechanical-fault path (not the pillory)
agent: steward
---
Quest: $ARGUMENTS

Reject a Quest's rendered Tribute **at `GATE`** and send it back to `WORKING` for
same-worktree remediation by a fresh (or the existing idle) Serf session. This is the
Gatekeeper's mechanical-fault-isolation path during Cog Ship packing — a merge
regression or failing test is a small mechanical fix in a known place, not an
architecture problem, so the worktree is NOT frozen and no `PUNISHED` ceremony runs.
Value/scope/duplication rejections belong exclusively to a review/audit role's full
judgment call (see `.kilo/commands/pillory.md`); this command is for integration
failures only. Uses only commands that already exist — no dedicated CLI subcommand
is needed for this path.

1. **Isolate the Fault (before rejecting):**
   - Bisect/re-test the Cog Ship pack to pinpoint the exact commit / Quest that broke
     the build.
   - Back out the offending branch from the station branch (`the-gatehouse/<station>`),
     re-merge only the clean Quests, and let the clean pack continue to promotion.

2. **Record the Rejection in the Quest's Charter:**
   ```
   python3 .court/engine/cli.py set-section <id> "Gatekeeper Review" --content "### ❌ Gatekeeper Rejection (Cog Ship Integration Failure)\n- **Failing Test Command**: `<cmd>`\n- **Error Traceback**:\n\`\`\`\n<traceback>\n\`\`\`\n- **Root Cause / Regression**: <details>\n- **Required Remediation**: <actionable steps>"
   python3 .court/engine/cli.py advance <id> WORKING --note "Gatekeeper rejected tribute: <one-line reason> (remediation in same worktree)"
   ```

3. **Dispatch a Fresh Serf Session in the SAME Worktree:**
   - Using `agent_manager`, start a new Serf session in the Quest's existing worktree
     (or prompt the existing idle Serf session) with
     `.court/templates/serf_remediation_prompt.md`, filled with the exact failing
     command, traceback, and required fixes.

4. **Serf Remediation Contract (what the fresh Serf does):**
   - Fix the root cause in place; run the exact failing test locally to a clean exit.
   - Confirm a clean tree (`git status --porcelain`) and zero behind-drift vs `castle`.
   - Append fix notes to `# Tribute Rendered` (never rewrite the original claim).

5. **Re-queue:**
   - The Quest re-enters the normal pipeline from `WORKING` (`/levy` -> Master of Coin
     -> `GATE`). No pillory linkage is created and no successor Quest is chartered for
     this path.

6. **Escalate If Not Mechanical:**
   - If inspection reveals the failure is architectural (wrong approach, duplicated
     capability, scope problem), STOP: report back to the Steward instead of running
     `/reject_tribute` again. Architecture problems go through the full pillory path
     with Decrees, not same-worktree patching.
