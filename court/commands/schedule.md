---
description: Why is this quest waiting? Per-quest eligibility verdicts from the Night's Watch planner (ACTION_NEEDED / QUEUED_AUTO / HELD), with constraint class
agent: steward
---
Arguments: $ARGUMENTS

Surface WHY an eligible-but-unadvanced Quest has not advanced:

`python3 -m court.cli schedule [<quest_id>] [--status TR,GR] [--json]`

1. Runs the SAME planner inputs the live Night's Watch uses (`court status
   --json` snapshot, persisted watch state, live worker census, recorded
   policy) through `court.nightwatch.plan` — it never reimplements the reason
   tracker and never executes a spawn.
2. For every quest in the selected scope it prints the verdict:
   - `ACTION_NEEDED` — a human/Steward decision is blocking (escalated: merge
     conflict, coin/goad ceiling, pending Audience, no usable worktree).
   - `QUEUED_AUTO` — the watch WILL advance it once a timer clears (cooldown,
     failed-action retry backoff).
   - `HELD_BY_POLICY` / `HELD_CAPACITY` — the watch is configured not to act,
     or a worker/budget ceiling is momentarily full.
   - `IN_FLIGHT` — a live worker owns it, or the planner would act this tick.
3. Each reason is classified by constraint: `HUMAN`, `LOCAL_CONCURRENCY`
   (RAM ceiling / worktree occupancy — local memory/CPU), `LOCAL_BUDGET`
   (hourly/count caps), `PROVIDER_TRANSIENT` (rate-limit/retry backoff after
   a failed action), `TEMPORARY_COOLDOWN` (local policy timer).
4. `--json` emits the full verdict list for programmatic use.

Also raised in the AI index: queued-but-unadvanced `TRIBUTE_READY` quests are
the classic scheduling bottleneck this command diagnoses.