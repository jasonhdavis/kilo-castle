---
description: Full Court dashboard - active Quests, Serfs, live task progress, and pending Audiences
agent: steward
---
Arguments: $ARGUMENTS

Reconstruct current state using single-shot deterministic Court status:

1. Run `python3 -m court.cli status --sync $ARGUMENTS`
   *(Pass `--tree` for hierarchical epic tree view, or `--json` for structured data).*
2. The deterministic CLI output directly yields live worktree task progress `[Tasks: X/Y (%)]`, git dirtiness, tribute completeness, active phase, and post-deploy commutations in a single command.
3. Present the Observer dashboard shape to M'Lord, covering the pipeline sections in strict development funnel order:
   - 📝 **PLANNING ({N}) — keep working with `/plan`** (`PLANNED`): scoped and chartered on paper, no worktree yet.
   - 📋 **OPEN ({N}) — ready for `/plot` or `/charter`** (`OPEN`): raw/unscoped backlog. Unassigned Warden/Sentry tickets are summarized compactly.
   - 📜 **CHARTERED ({N}) — ready to start or goad** (`CHARTERED` / `DISPATCHED`): worktree spawned, serf assigned, waiting for work to commence.
   - ⚔️ **QUESTING ({N}) — `/goad` idle serfs or `/levy` to conduct tribute audit** (`QUESTING` / `WORKING`): active serfs in the field. Always reported (if 0, report none active).
   - 👇 **DEMOTED ({N})**: demoted side-state.
   - 🔒 **PUNISHED ({N}) — frozen side-state; charter successor with `/charter <new_id> --pillory-of <id>`** (`PUNISHED`): pilloried quest awaiting successor.
   - ⏸️ **HELD ({N}) — blocked on `/audience`** (`HELD`): awaiting royal decision.
   - 🪙 **TRIBUTE_READY ({N}) — ready for /levy** (`TRIBUTE_READY`): Serf has rendered Tribute; ready for Master of Coin audit (never titled "REVIEW").
   - 🛡️ **Tribute at the GATE ready for /collect ({N})** (`GATE`): Master-of-Coin-approved; standing by for Gatekeeper convoy packing.
   - 🚢 **Cogships Ready ({N}) — launch with /ship**: landed tributes staged on `castle`, ready to sail to production (`main`).
   - 🪦 **READY_TO_RAZE ({N}) — ready for /teardown** (`READY_TO_RAZE`): merged to `castle`, awaiting worktree pruning.
   - ⚡ **COMMUTATIONS REQUIRED ({N}) — post-deployment actions for the Steward**: operational actions (migrations, backfills, worker restarts, setting toggles) for landed/shipped quests. Quests whose commutation has been logged as completed in `## Cogship Log` (via `python3 -m court.cli commute <id> --note "..."`) drop out of this list and appear on a collapsed `✅ Commutations Done (N) — logged in Cogship Log: <ids>` line.
4. Badge format: `[Phase: ...] [Tasks: X/Y (%)] (Tribute: X/Y sections) [CLEAN/DIRTY]` (no ahead/behind).
5. Footer:
   - `Hear the quest ballads with /bard /atone /coffers /tally and /murmur`
   - `Ready to Ship - Use /ship --confirm to launch` (when Cogships ready)
   - `Note: X orphaned worktrees exist in Agent Manager...`

Keep it concise and high-signal. Do not make redundant tool calls or loop over individual files.
