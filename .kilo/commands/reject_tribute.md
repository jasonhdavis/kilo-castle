---
description: Reject a Quest's tribute at GATE for same-worktree Serf remediation (Gatekeeper integration-failure path, the one exception to the pillory)
agent: steward
---
Quest: $ARGUMENTS

Reject a Quest's rendered Tribute **at `GATE`** and send it back to `WORKING` for
same-worktree remediation by a fresh Serf session. This is the Gatekeeper's
mechanical-fault-isolation path during Cog Ship packing — **the one deliberate
exception to the Pillory protocol**: a merge regression or failing test is a small
mechanical fix in a known place, not an architecture problem, so the worktree is
NOT frozen and no `PUNISHED` ceremony runs. (Value/scope/duplication rejections
belong exclusively to the Master of Coin's one-shot audit at `TRIBUTE_READY` -> `court pillory`;
this command is for integration failures only. The Gatekeeper follows these identical
steps autonomously from inside its convoy worktree (the solo Quest's own worktree for a
size-1 convoy, or the ephemeral `the-gatehouse/<cogship_id>` worktree for a convoy of
size > 1) — see `gatekeeper_review_prompt.md`.)

1. **Isolate the Fault (before rejecting)**:
   - Bisect/re-test the Cog Ship pack to pinpoint the exact commit / Quest that broke the build.
   - For a convoy of size > 1: back out the offending branch from the ephemeral
     `the-gatehouse/<cogship_id>` branch, re-merge only the clean Quests, and let the clean
     pack continue to promotion. For a convoy of size 1, there's nothing to isolate — the
     whole convoy IS the failing Quest, so just return it to `WORKING` (step 2 below).

2. **Record the Rejection in the Quest's Charter** (`## Cogship Log`, declarative fields):
   ```bash
   python3 -m court.cli set-section <id> "Cogship Log" --append --content "- **Result:** REJECTED (Cog Ship integration failure)
   - **Test Command:** \`<failing command>\`
   - **Error Traceback:** <traceback, fenced>
   - **Root Cause / Regression:** <details>
   - **Required Remediation:** <actionable steps>"
   python3 -m court.cli advance <id> WORKING --note "Gatekeeper rejected tribute: <one-line reason> (remediation in same worktree)"
   ```

3. **Dispatch a Fresh Serf Session in the SAME Worktree**:
   - Using `agent_manager`, start a new Serf session in the Quest's existing worktree
     (or prompt the existing idle Serf session) with `.court/templates/serf_remediation_prompt.md`,
     filled with the exact failing command, traceback, and required fixes.
   - Model: the standard Serf model (`z-ai/glm-5.3-flash`); this is mechanical repair work.

4. **Serf Remediation Contract** (what the fresh Serf does):
   - Fix the root cause in place; run the exact failing test locally to exit 0.
   - Clean tree (`git status --porcelain`) and zero behind-drift vs `castle`.
   - **Append** fix notes to `Tribute Rendered` (`--append`) — never rewrite the original claim.

5. **Re-queue**:
   - The Quest re-enters the normal pipeline from `WORKING` (Levy -> Master of Coin's
     next one-shot audit -> `GATE`). No pillory linkage is created and no successor
     Quest is chartered for this path.

6. **Escalate If Not Mechanical**:
   - If inspection reveals the failure is architectural (wrong approach, duplicated
     capability, scope problem), STOP: report back to the Steward instead of running
     `/reject_tribute`. Architecture problems go back through the Master of Coin's
     pillory path with Decrees, not through same-worktree patching.
