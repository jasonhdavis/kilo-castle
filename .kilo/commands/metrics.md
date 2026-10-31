---
description: Read-only per-Quest rollup of cost, wall-clock, turns, and goads for model bake-offs
agent: steward
---
Arguments: $ARGUMENTS

Follow the Metrics Protocol:
1. Run `python3 -m court.cli metrics $ARGUMENTS` to compute the read-only per-Quest rollup from the local `kilo.db` (session cost, wall-clock timestamps, turn counts) and the Court's own goad ledger (worktree `.kilo/*.log` goad markers).
2. Use `--by-model` to aggregate the same four measurements by model when comparing bake-off arms, `--sort cost|wall|turns|goads` to rank Quests, and `--json` for machine consumption.
3. This command is strictly read-only — it never writes a Quest file, event log, or git ref. Present the four measurements (cost, wall-clock, turns, goads) to M'Lord so a model switch is judged on data, not vibes.
