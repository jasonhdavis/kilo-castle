---
description: The Warden's hunting-grounds patrol: last survey, pending Warden Reports, and realm compliance health
agent: steward
---
Arguments: $ARGUMENTS

Reconstruct Ward state using single-shot deterministic Court status:

1. Run `python3 -m court.cli ward $ARGUMENTS`
   *(Pass `--check-fresh` to also call the decoupled Sentry/Fly log harness for fresh unresolved
   issue detection — network/credentials required. Pass `--json` for structured data.)*
2. The deterministic CLI output directly yields the last patrol survey timestamp, pending
   Warden Reports awaiting Steward chartering, and realm-wide compliance health (base drift,
   dirty worktrees, tribute completeness) across every in-flight Quest — in a single command.
3. Present the Ward dashboard shape to M'Lord:
   - 🏹 **Last Survey** (when the hunting grounds were last patrolled)
   - 📋 **Warden Reports Awaiting Charter** (ready to become a `Bug fix` Quest)
   - 🛡️ **Realm Compliance Health** (non-compliant Quests, dirty/behind worktrees)
   - 🔍 **Fresh Issues** (only if `--check-fresh` was passed)

## Dispatching a New Warden Patrol

To start a fresh hunting-grounds patrol (rather than just reading the last one):
1. Spawn an Agent Manager worktree session using the **Warden** agent persona
   (`agent: warden`) on a dedicated branch: `ward/hunting-grounds` or `ward/<date>-patrol`.
2. Use `.court/templates/warden_dispatch_prompt.md` as the initial prompt, filling in the
   patrol scope.
3. The Warden files 5-part Warden Reports to `.court/ward/reports/` and updates
   `.court/ward/WARDENS_LOG.md` before finishing.
4. Once reports are filed, `court ward` will list them under "Warden Reports Awaiting
   Charter" — review each and `/charter` a focused production Quest (typically `Bug fix`)
   directly from its "Proposed Fix / Remit" section.

Keep it concise and high-signal. Do not make redundant tool calls or loop over individual files.
