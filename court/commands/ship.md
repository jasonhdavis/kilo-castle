---
description: Alias for /cog ship — summarize the tribute convoy entering the castle and prepare the castle -> main deployment report
agent: steward
---
Arguments: $ARGUMENTS

Alias for `/cog ship`. Run `python3 -m court.cli ship $ARGUMENTS` and present the
combined Bard/Coffers/Tally/Atone/Murmur/Commutation deployment convoy summary to M'Lord, per the Cog Ship
Protocol in `.kilo/prompts/steward.md`.

Without `--confirm` this is a read-only report. With `--confirm` the command executes the
production promotion: merge `castle` -> `main` and push `main` to `origin` (triggering the
production deployment pipeline). Use `--dry-run` with `--confirm` to run only the preflight safety
checks (production checkout located, correct branch, clean tree, push fast-forward) without
mutating anything. Only pass `--confirm` when M'Lord has explicitly approved the promotion.

### Steward's Post-Deployment Commutation Duty
Following successful deployment to production:
1. **Review the Commutation Manifest**: Inspect the `⚡ THE COMMUTATION MANIFEST` generated during `/ship`.
2. **Execute Kingdom Actions**: The Steward is responsible for executing any data backfills, worker reboots/boot log checks, environment variable toggles, and marking issues resolved in error tracking.
3. **Record in Quest Charter**: Record completion with the dedicated verb — `python3 -m court.cli commute <id> --note "<what was executed>"` — which appends a dated `**Commutation (YYYY-MM-DD):**` entry to the Quest's `## Cogship Log`. That entry is the completion marker: it drops the Quest out of `court status`'s ⚡ COMMUTATIONS REQUIRED list and the next `/ship` Commutation Manifest, moving it to the collapsed `✅ Commutations Done (N)` line while preserving the Master of Coin's audit history unchanged.
