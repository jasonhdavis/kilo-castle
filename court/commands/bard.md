---
description: Review ballads across an Epic, App, or recent Quests into a narrative summary
agent: steward
---
Arguments: $ARGUMENTS

`rollup` is filter-only (`--epic`/`--app`/`--status`/`--cogship`/`--include-archived`) —
it has **no single-Quest-ID mode** (deliberate, per Q185's Universal Targeting Convention).
Passing a bare Quest ID straight through as $ARGUMENTS is a guaranteed `unrecognized
arguments` crash — never improvise around that crash by dropping the target filter and
falling back to an unfiltered/`--include-archived` rollup; that silently returns every
Quest in the ledger instead of the one M'Lord asked about, which is worse than erroring.

Follow the Bard Protocol:
1. Determine what $ARGUMENTS actually is:
   - **A single Quest ID** (e.g. `Q177`, `177`, or a full slug like
     `Q177-Apps-Crm-Ldi-Brand-Verification`) → run
     `python3 -m court.cli show <id>` instead of `rollup`. Read that Quest's own
     `Tribute Rendered` section directly out of the `show` output — `rollup` was never
     designed for this case. If `Tribute Rendered` (or the specific Ballad subsection) is
     empty, say so plainly ("no Ballad rendered yet — Quest is <status>") instead of
     substituting unrelated Quests' ballads.
   - **A genuine filter set** (`--epic <n>`, `--app <name>`, `--status <s>`, or no
     arguments at all for "recent Quests") → run
     `python3 -m court.cli rollup --section ballad $ARGUMENTS` as-is.
   - If unsure which case applies, ask rather than guessing — do not silently widen the
     scope.
   - Landmine if you do use `--epic <n>`: it matches the *exact* zero-padded numeric ID
     prefix (`Q004` needs `--epic 004`, not `--epic 4`). Prefer `show <id>` for a single
     Quest so this never matters.
2. Synthesize the extracted ballad(s) into a clean, executive narrative arc:
   - What challenges were encountered.
   - What architectural decisions were made.
   - What has been shipped and transformed.
3. Present the chronicled story concisely to M'Lord.
